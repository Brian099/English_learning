'use strict';

/* 学生端：配对 → 今日任务 → 播放器（听/看双模式 + 硬字幕遮挡 + 有效时长统计） */

const state = {
  token: localStorage.getItem('student_token') || '',
  student: null,
  today: null,
  index: 0,
  loop: false,
  rate: 1.0,
  mode: localStorage.getItem('player_mode') || 'video',   // video（默认，以视频为主）| audio
  volume: Number(localStorage.getItem('volume') ?? 100),   // 0-100
  voiceBoost: false,                                        // 人声增强（Web Audio）
  maskOn: localStorage.getItem('mask_on') === '1',          // 默认不遮，家长可开
  maskHeight: Number(localStorage.getItem('mask_height') || 20),
  session: null,
  queue: [],
};

const TICK_MS = 1000;
const MAX_RATE_FOR_CREDIT = 1.25;   // 超过这个倍速不算有效收听
const MIN_COMPLETE_RATIO = 0.9;     // 听满 90% 语音时长算完成
const MIN_COMPLETE_MS = 10_000;     // 至少 10 秒，防止点开就退

/* ------------------------------------------------------------ 基础工具 */

function uuid() {
  if (window.crypto && typeof window.crypto.randomUUID === 'function') return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  (window.crypto || {}).getRandomValues?.(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function fmtTime(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return '0:00';
  const total = Math.floor(seconds);
  return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), 2000);
}

async function api(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    forgetToken();
    throw new Error('需要重新配对');
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

function forgetToken(hint) {
  state.token = '';
  localStorage.removeItem('student_token');
  const errorEl = document.getElementById('pair-error');
  if (errorEl) errorEl.textContent = hint || '';
  showScreen('pair');
}

/** 令牌是否还有效。媒体报错时用它区分"地址问题"和"配对过期"。 */
async function tokenValid() {
  try {
    await api('/api/me');
    return true;
  } catch (err) {
    return false;
  }
}

/** 停掉正在播放的媒体。离开播放器时必须调用，否则回到列表还在响。 */
function stopPlayback() {
  ['audio', 'video'].forEach((id) => {
    const el = document.getElementById(id);
    if (el && !el.paused) {
      try { el.pause(); } catch (err) { /* 忽略 */ }
    }
  });
  toggleSheet(false);
}

function showScreen(name) {
  // 离开播放器就停播：返回列表、配对失效、换设备都走这里
  if (name !== 'player') stopPlayback();
  ['pair', 'home', 'player'].forEach((screen) => {
    document.getElementById(`screen-${screen}`).classList.toggle('hidden', screen !== name);
  });
}

/* ------------------------------------------------------------ 配对 */

document.getElementById('pair-btn').addEventListener('click', pair);
document.getElementById('pair-code').addEventListener('keydown', (event) => {
  if (event.key === 'Enter') pair();
});

async function pair() {
  const input = document.getElementById('pair-code');
  const code = input.value.trim();
  const errorEl = document.getElementById('pair-error');
  errorEl.textContent = '';
  if (code.length !== 6) {
    errorEl.textContent = '请输入 6 位数字';
    return;
  }
  try {
    const data = await api('/api/devices/pair', {
      method: 'POST',
      body: JSON.stringify({
        code,
        device_name: '学生网页端',
        platform: (navigator.userAgentData?.platform || navigator.platform || 'web'),
      }),
    });
    state.token = data.token;
    localStorage.setItem('student_token', data.token);
    await loadHome();
  } catch (err) {
    errorEl.textContent = err.message;
  }
}

/* ------------------------------------------------------------ 首页 */

document.getElementById('switch-device').addEventListener('click', () => {
  if (window.confirm('要断开这台设备吗？需要家长重新给配对码')) forgetToken();
});

document.querySelectorAll('.tab').forEach((tab) => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.tab').forEach((t) => t.classList.toggle('active', t === tab));
    const isHistory = tab.dataset.view === 'history';
    document.getElementById('history-view').classList.toggle('hidden', !isHistory);
    document.querySelectorAll('#task-list, .section-title').forEach((el) => {
      el.classList.toggle('hidden', isHistory);
    });
    if (isHistory) renderHistory();
  });
});

document.getElementById('start-btn').addEventListener('click', () => {
  const items = state.today?.items || [];
  if (!items.length) {
    toast('今天还没有任务');
    return;
  }
  const firstUndone = items.findIndex((item) => !item.completed);
  openPlayer(firstUndone >= 0 ? firstUndone : 0);
});

async function loadHome() {
  try {
    const data = await api('/api/student/today');
    state.today = data;
    state.student = data.student;
    renderHome();
    showScreen('home');
    await flushQueue();
  } catch (err) {
    if (!state.token) {
      // api() 遇到 401 已经清掉令牌并切到配对页，这里补一句原因
      const hint = document.getElementById('pair-error');
      if (hint && !hint.textContent) hint.textContent = '配对已失效，请让家长重新生成配对码';
      return;
    }
    toast(err.message);
  }
}

function renderHome() {
  const data = state.today;
  const percent = data.target_min > 0 ? Math.min(1, data.effective_min / data.target_min) : 0;

  document.getElementById('hello-name').textContent = `你好，${data.student.name}`;
  document.getElementById('today-summary').textContent = data.effective_min > 0
    ? `今天已经听了 ${data.effective_min} 分钟，继续加油`
    : '今天还没有听哦';
  document.getElementById('target-min').textContent = data.target_min;
  document.getElementById('done-count').textContent =
    `已完成 ${data.items.filter((item) => item.completed).length} / ${data.items.length} 集`;

  const circumference = 327;
  document.getElementById('ring-fg').style.strokeDashoffset = String(circumference * (1 - percent));
  document.getElementById('ring-min').textContent = String(Math.round(data.effective_min));

  const list = document.getElementById('task-list');
  if (!data.items.length) {
    list.innerHTML = '<div class="task" style="grid-template-columns:1fr">' +
      '<div class="name">今天还没有任务</div></div>';
    return;
  }
  list.innerHTML = data.items.map((item, index) => `
    <div class="task ${item.completed ? 'done' : ''}" data-index="${index}">
      <div class="idx">${item.completed ? '✓' : index + 1}</div>
      <div class="name" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</div>
      <div class="dur">${fmtTime((item.duration_ms || 0) / 1000)}</div>
    </div>`).join('');

  list.querySelectorAll('.task[data-index]').forEach((node) => {
    node.addEventListener('click', () => openPlayer(Number(node.dataset.index)));
  });
}

async function renderHistory() {
  const root = document.getElementById('history-view');
  root.innerHTML = '<div class="row">加载中…</div>';
  try {
    const data = await api('/api/student/history?days=7');
    const max = Math.max(1, ...data.days.map((day) => day.effective_min));
    root.innerHTML = data.days.map((day) => `
      <div class="row">
        <span style="width:44px;color:var(--muted);font-size:12px">${day.day.slice(5)}</span>
        <div class="bar"><i style="width:${Math.round((day.effective_min / max) * 100)}%"></i></div>
        <span class="min">${day.effective_min} 分</span>
      </div>`).join('') +
      `<div class="row" style="justify-content:space-between">
         <span class="muted">近 7 天共 ${data.total_min} 分钟</span>
         <span class="muted">${data.active_days} 天有听</span>
       </div>`;
  } catch (err) {
    root.innerHTML = `<div class="row">${escapeHtml(err.message)}</div>`;
  }
}

/* ------------------------------------------------------------ 播放器 */

const audio = document.getElementById('audio');
const video = document.getElementById('video');

function activeEl() {
  return state.mode === 'video' ? video : audio;
}

function sourceFor(item) {
  const url = state.mode === 'video' ? item.video_url : item.audio_url;
  const sep = url.includes('?') ? '&' : '?';
  // <audio>/<video> 不能自定义请求头，令牌只能走查询参数
  return `${url}${sep}token=${encodeURIComponent(state.token)}`;
}

document.getElementById('player-back').addEventListener('click', async () => {
  await finalizeCurrent(false);
  await loadHome();
});

document.getElementById('btn-play').addEventListener('click', togglePlay);
document.getElementById('btn-prev').addEventListener('click', () => step(-1));
document.getElementById('btn-next').addEventListener('click', () => step(1));

document.getElementById('mode-switch').addEventListener('click', (event) => {
  const button = event.target.closest('[data-mode]');
  if (!button) return;
  switchMode(button.dataset.mode);
});

document.getElementById('btn-loop').addEventListener('click', () => {
  state.loop = !state.loop;
  audio.loop = state.loop;
  video.loop = state.loop;
  document.getElementById('btn-loop').classList.toggle('on', state.loop);
});

document.getElementById('btn-rate').addEventListener('click', () => {
  const order = [0.8, 1.0, 1.2];
  const next = order[(order.indexOf(state.rate) + 1) % order.length];
  state.rate = next;
  audio.playbackRate = next;
  video.playbackRate = next;
  document.getElementById('btn-rate').textContent = `${next.toFixed(1)}×`;
  document.getElementById('btn-rate').classList.toggle('on', next !== 1.0);
});

document.getElementById('btn-mask').addEventListener('click', () => {
  state.maskOn = !state.maskOn;
  localStorage.setItem('mask_on', state.maskOn ? '1' : '0');
  applyMask();
});

/* ------------------------------------------------------------ 音量 */

const volumeInput = document.getElementById('volume');
const volumeValue = document.getElementById('volume-value');

function applyVolume() {
  const value = Math.max(0, Math.min(100, state.volume));
  const base = value / 100;
  const gain = levelGain(currentItem());
  const el = activeEl();

  // 衰减（gain<1）直接用元素音量解决，不需要 Web Audio；
  // 放大（gain>1）才需要 Web Audio 的增益节点。
  el.volume = base * Math.min(gain, 1);
  const boosted = setDspGain(Math.max(gain, 1));
  if (!boosted) el.volume = base;      // 需要放大但环境不支持，保持原音量

  // 提示要反映"实际生效"的增益，放大失败时不能显示成已校正
  const applied = boosted ? gain : Math.min(gain, 1);
  dsp.gainDb = 20 * Math.log10(applied);

  volumeInput.value = String(value);
  volumeValue.textContent = `${value}%`;
  updateVolumeTip();
}

/** 提示行优先级：响度校正 > 听力保护提醒 > 空 */
function updateVolumeTip() {
  const tip = document.getElementById('volume-tip');
  if (dsp.enabled && Math.abs(dsp.gainDb) >= 0.8) {
    tip.textContent = `响度校正 ${dsp.gainDb > 0 ? '+' : ''}${dsp.gainDb.toFixed(1)} dB`;
    return;
  }
  if (state.volume >= 100) {
    tip.textContent = '已到最大音量，建议 80% 以内保护听力';
    return;
  }
  tip.textContent = '';
}

volumeInput.addEventListener('input', (event) => {
  state.volume = Number(event.target.value);
  localStorage.setItem('volume', String(state.volume));
  applyVolume();
});

/* ------------------------------------------------------------ 音频处理链 */

// 一条 Web Audio 链同时干两件事：
//   source → gain(自动音量) → compressor(人声增强) → destination
//                                 └→ analyser（测量实际输出）
// 自动音量 = 实时 AGC：每 200ms 量一次 RMS，慢慢把音量拉到同一目标，
// 所以不同来源、不同响度的素材播出来听感一致。

const TARGET_LUFS = -16;            // 播放目标响度
const PEAK_CEILING = -1;            // 放大后真峰值上限（dBFS）
const AUTO_LEVEL_MAX_DB = 18;       // 最大修正幅度

const dsp = {
  enabled: localStorage.getItem('auto_level') !== '0',   // 默认开启
  ctx: null,
  graphs: new Map(),
  gainDb: 0,
};

function dspAvailable() {
  return Boolean(window.AudioContext || window.webkitAudioContext);
}

/** 给某个媒体元素建处理链（每个元素只能建一次，之后复用）。 */
function dspGraph(el) {
  if (!dspAvailable()) return null;
  let graph = dsp.graphs.get(el);
  if (graph) return graph;

  try {
    if (!dsp.ctx) dsp.ctx = new (window.AudioContext || window.webkitAudioContext)();
    const source = dsp.ctx.createMediaElementSource(el);
    const gain = dsp.ctx.createGain();
    const compressor = dsp.ctx.createDynamicsCompressor();
    const analyser = dsp.ctx.createAnalyser();
    analyser.fftSize = 2048;

    source.connect(gain);
    gain.connect(compressor);
    compressor.connect(analyser);
    compressor.connect(dsp.ctx.destination);

    graph = { gain, compressor, analyser, buf: new Float32Array(analyser.fftSize) };
    dsp.graphs.set(el, graph);
    applyVoiceBoost();
    return graph;
  } catch (err) {
    console.warn('Web Audio 初始化失败', err);
    return null;
  }
}

function dspResetGain() {
  dsp.gainDb = 0;
  dsp.graphs.forEach((graph) => {
    graph.gain.gain.setTargetAtTime(1, dsp.ctx ? dsp.ctx.currentTime : 0, 0.05);
  });
}

/**
 * 按入库时记录的响度算一个静态增益。
 *
 * 入库流水线已经做过归一化（目标 −16 LUFS），并把"归一化后的实测响度"
 * 写进了 audio_lufs；看模式播的是原始文件，用 lufs。
 * 播放端只需要 gain = 10^((目标 − 实测)/20)，一次算好，不做运行时测量。
 */
function levelGain(item) {
  if (!dsp.enabled || !item) return 1;
  const measured = state.mode === 'video' ? item.lufs : item.audio_lufs;
  if (typeof measured !== 'number' || !Number.isFinite(measured)) return 1;

  let db = TARGET_LUFS - measured;
  // 峰值保护：放大后真峰值不能超过 PEAK_CEILING，否则会削波
  const peak = item.lufs_tp;
  if (typeof peak === 'number' && Number.isFinite(peak)) {
    db = Math.min(db, PEAK_CEILING - peak);
  }
  db = Math.max(-AUTO_LEVEL_MAX_DB, Math.min(AUTO_LEVEL_MAX_DB, db));
  return Math.pow(10, db / 20);
}

/** 把 >1 的增益交给 Web Audio；<=1 的部分直接用元素音量解决，不用建链。 */
function setDspGain(factor) {
  if (factor <= 1.0001) {
    dsp.graphs.forEach((graph) => {
      graph.gain.gain.setTargetAtTime(1, dsp.ctx ? dsp.ctx.currentTime : 0, 0.05);
    });
    dsp.gainDb = 0;
    return true;
  }
  if (!dspAvailable()) return false;
  const graph = dspGraph(activeEl());
  if (!graph) return false;
  if (dsp.ctx && dsp.ctx.state === 'suspended') dsp.ctx.resume().catch(() => {});
  graph.gain.gain.setTargetAtTime(factor, dsp.ctx.currentTime, 0.05);
  dsp.gainDb = 20 * Math.log10(factor);
  return true;
}

function updateAutoLevelButton() {
  document.getElementById('btn-autolevel').classList.toggle('on', dsp.enabled);
}

document.getElementById('btn-autolevel').addEventListener('click', () => {
  dsp.enabled = !dsp.enabled;
  localStorage.setItem('auto_level', dsp.enabled ? '1' : '0');
  updateAutoLevelButton();
  applyVolume();
  toast(dsp.enabled ? '自动音量已开：按入库响度校正' : '自动音量已关：音量取决于素材本身');
});

/* ------------------------------------------------------------ 人声增强 */

function applyVoiceBoost() {
  const button = document.getElementById('btn-voice');
  dsp.graphs.forEach((graph) => {
    const { compressor, gain } = graph;
    if (state.voiceBoost) {
      compressor.threshold.value = -24;
      compressor.knee.value = 12;
      compressor.ratio.value = 4;
      compressor.attack.value = 0.003;
      compressor.release.value = 0.25;
      gain.gain.value = Math.max(gain.gain.value, 1);
    } else {
      compressor.threshold.value = 0;
      compressor.knee.value = 0;
      compressor.ratio.value = 1;
    }
  });
  button.classList.toggle('on', state.voiceBoost);
}

document.getElementById('btn-voice').addEventListener('click', async () => {
  if (!state.voiceBoost) {
    if (!dspAvailable()) {
      toast('这台设备不支持人声增强');
      return;
    }
    state.voiceBoost = true;
    const graph = dspGraph(activeEl());
    if (!graph) {
      state.voiceBoost = false;
      toast('这台设备不支持人声增强');
      return;
    }
    if (dsp.ctx && dsp.ctx.state === 'suspended') {
      try { await dsp.ctx.resume(); } catch (err) { /* 忽略 */ }
    }
    applyVoiceBoost();
    document.getElementById('volume-tip').textContent = '人声增强已开，可能影响熄屏后台播放';
    toast('已开启人声增强');
  } else {
    state.voiceBoost = false;
    applyVoiceBoost();
    applyVolume();
    toast('已关闭人声增强');
  }
});

document.getElementById('mask-height').addEventListener('input', (event) => {
  state.maskHeight = Number(event.target.value);
  localStorage.setItem('mask_height', String(state.maskHeight));
  applyMask();
});

/* 遮罩只在"看"模式下有意义：听模式没有画面，也就没有字幕 */
function applyMask() {
  const mask = document.getElementById('subtitle-mask');
  const isVideo = state.mode === 'video';
  const visible = isVideo && state.maskOn;

  mask.classList.toggle('off', !visible);
  mask.style.height = `${state.maskHeight}%`;

  const button = document.getElementById('btn-mask');
  button.classList.toggle('hidden', !isVideo);
  button.classList.toggle('on', visible);
  document.getElementById('mask-slider-wrap').classList.toggle('hidden', !visible);
  document.getElementById('mask-height').value = String(state.maskHeight);
  document.getElementById('mask-value').textContent = `${state.maskHeight}%`;
}

function renderMode() {
  document.querySelectorAll('#mode-switch .mode-btn').forEach((button) => {
    button.classList.toggle('active', button.dataset.mode === state.mode);
  });
  const isVideo = state.mode === 'video';
  document.getElementById('cover-art').classList.toggle('hidden', isVideo);
  video.classList.toggle('hidden', !isVideo);
  document.getElementById('player-tip').textContent = isVideo
    ? '上滑下一集 · 下滑上一集'
    : '熄屏也能继续听哦';
  applyMask();
}

/* ------------------------------------------------------------ 展开面板 */

function toggleSheet(force) {
  const feed = document.getElementById('screen-player');
  const open = force === undefined ? !feed.classList.contains('expanded') : force;
  feed.classList.toggle('expanded', open);
  document.getElementById('btn-more').classList.toggle('on', open);
}

document.getElementById('btn-more').addEventListener('click', () => toggleSheet());

/* ------------------------------------------------------------ 上下滑切换 */

const stage = document.getElementById('stage');
let touchStartY = null;
let wheelLock = false;

function slideTo(delta) {
  if (delta === 0) return;
  stage.classList.add(delta > 0 ? 'slide-up' : 'slide-down');
  setTimeout(() => {
    stage.classList.remove('slide-up', 'slide-down');
    step(delta);
  }, 180);
}

stage.addEventListener('touchstart', (event) => {
  if (event.touches.length !== 1) return;
  touchStartY = event.touches[0].clientY;
}, { passive: true });

stage.addEventListener('touchend', (event) => {
  if (touchStartY === null) return;
  const endY = event.changedTouches[0]?.clientY ?? touchStartY;
  const dy = touchStartY - endY;      // 上滑为正
  touchStartY = null;
  if (Math.abs(dy) < 55) return;      // 太小当点击
  slideTo(dy > 0 ? 1 : -1);
}, { passive: true });

// 桌面端滚轮
stage.addEventListener('wheel', (event) => {
  if (wheelLock || Math.abs(event.deltaY) < 12) return;
  wheelLock = true;
  slideTo(event.deltaY > 0 ? 1 : -1);
  setTimeout(() => { wheelLock = false; }, 600);
}, { passive: true });

// 键盘上下键
window.addEventListener('keydown', (event) => {
  if (document.getElementById('screen-player').classList.contains('hidden')) return;
  if (event.key === 'ArrowDown') { event.preventDefault(); slideTo(1); }
  if (event.key === 'ArrowUp') { event.preventDefault(); slideTo(-1); }
  if (event.key === 'Escape') toggleSheet(false);
});

function currentItem() {
  return state.today?.items?.[state.index] || null;
}

/**
 * 打开某一集。
 *
 * 注意：这里**必须是同步函数**，并且 play() 要在同一次调用里执行。
 * 浏览器（Chrome/Safari）的自动播放策略要求 play() 处在用户手势的调用栈里，
 * 一旦中间插了 await，手势授权就丢了，play() 会被 NotAllowedError 拒绝，
 * 表现为"点了没反应、也不发媒体请求"。
 * 所以：先同步把上一段结算数据摘出来，同步开播，再异步上报。
 */
function openPlayer(index) {
  const items = state.today?.items || [];
  if (!items[index]) return;

  const previousSession = state.session;
  const previousItem = currentItem();
  state.session = null;   // 立刻摘掉，避免计时器把上一段算到新一集头上

  state.index = index;
  const item = items[index];

  document.getElementById('player-title').textContent = item.title;
  document.getElementById('player-meta').textContent =
    `${fmtTime((item.duration_ms || 0) / 1000)}${item.level ? ' · ' + item.level : ''}`;
  document.getElementById('repair-btn').classList.add('hidden');

  // 顶部 HUD
  document.getElementById('hud-name').textContent = item.title;
  document.getElementById('hud-progress').textContent =
    `${index + 1} / ${items.length}${item.completed ? ' · 已完成' : ''}`;
  toggleSheet(false);

  audio.pause();
  video.pause();
  audio.removeAttribute('src');
  video.removeAttribute('src');

  const el = activeEl();
  el.src = sourceFor(item);
  el.playbackRate = state.rate;
  el.loop = state.loop;

  renderMode();
  applyVolume();
  showScreen('player');
  resetSession(item.video_id);
  updateSeek();

  // 同步开播，保住用户手势
  const played = el.play();
  if (played && typeof played.catch === 'function') {
    played.catch((err) => {
      document.getElementById('player-tip').textContent = '点一下播放键开始';
      if (err && err.name && err.name !== 'AbortError') {
        console.warn('play() 被拒绝:', err.name, err.message);
      }
    });
  }

  setupMediaSession(item);

  if (previousSession && previousItem && !previousSession.finalized) {
    finalizeSession(previousSession, previousItem, false).catch(() => {});
  }
}

async function switchMode(mode) {
  if (mode === state.mode || (mode !== 'audio' && mode !== 'video')) return;
  const item = currentItem();
  if (!item) return;

  const prev = activeEl();
  const position = prev.currentTime || 0;
  const wasPlaying = !prev.paused;

  prev.pause();
  prev.removeAttribute('src');

  state.mode = mode;
  localStorage.setItem('player_mode', mode);

  const el = activeEl();
  el.src = sourceFor(item);
  el.playbackRate = state.rate;
  el.loop = state.loop;
  renderMode();
  applyVolume();     // 切换模式后响度来源变了（lufs ↔ audio_lufs），重算校正

  const resume = () => {
    try {
      el.currentTime = position;
    } catch (err) { /* 忽略 */ }
    if (wasPlaying) el.play().catch(() => {});
  };
  if (el.readyState >= 1) resume();
  else el.addEventListener('loadedmetadata', resume, { once: true });

  if (wasPlaying) el.play().catch(() => {});
  updateSeek();
  toast(mode === 'video' ? '切换到「看」，熄屏会暂停' : '切换到「听」，熄屏继续播');
}

function resetSession(videoId) {
  state.session = {
    videoId,
    startedAt: new Date().toISOString(),
    effectiveMs: 0,
    maxPosMs: 0,
    seeks: 0,
    finalized: false,
  };
}

function togglePlay() {
  const el = activeEl();
  if (el.paused) el.play().catch(() => {});
  else el.pause();
}

async function step(delta) {
  const items = state.today?.items || [];
  const next = state.index + delta;
  if (next < 0 || next >= items.length) {
    toast(delta > 0 ? '已经最后一集啦' : '已经是第一集啦');
    return;
  }
  await openPlayer(next);
}

/* 有效时长：只在"真的在播、没在拖、倍速不高"时累加 */
setInterval(() => {
  const session = state.session;
  if (!session || session.finalized) return;
  const el = activeEl();
  if (el.paused || el.seeking) return;
  if (el.playbackRate > MAX_RATE_FOR_CREDIT) return;
  session.effectiveMs += TICK_MS;
  session.maxPosMs = Math.max(session.maxPosMs, Math.round(el.currentTime * 1000));
}, TICK_MS);

/* 两个元素共用一套监听，只有"当前模式"的那个生效 */
[audio, video].forEach((el) => {
  el.addEventListener('seeking', () => {
    if (el === activeEl() && state.session) state.session.seeks += 1;
  });
  el.addEventListener('timeupdate', () => {
    if (el !== activeEl()) return;
    if (state.session) {
      state.session.maxPosMs = Math.max(state.session.maxPosMs, Math.round(el.currentTime * 1000));
    }
    updateSeek();
  });
  el.addEventListener('play', () => {
    if (el !== activeEl()) return;
    document.getElementById('btn-play').textContent = '❚❚';
    document.getElementById('screen-player').classList.remove('paused');
  });
  el.addEventListener('pause', () => {
    if (el !== activeEl()) return;
    document.getElementById('btn-play').textContent = '▶';
    document.getElementById('screen-player').classList.add('paused');
  });
  el.addEventListener('ended', async () => {
    if (el !== activeEl()) return;
    await finalizeCurrent(true);
    const items = state.today?.items || [];
    if (state.index + 1 < items.length) {
      toast('这集听完啦，下一集');
      setTimeout(() => openPlayer(state.index + 1), 800);
    } else {
      toast('今天的任务全部完成，太棒了！');
      await loadHome();
    }
  });
});

function updateSeek() {
  const el = activeEl();
  const duration = el.duration || (currentItem()?.duration_ms || 0) / 1000;
  const current = el.currentTime || 0;
  const percent = duration > 0 ? Math.min(100, (current / duration) * 100) : 0;
  document.getElementById('seek-fill').style.width = `${percent}%`;
  document.getElementById('cur-time').textContent = fmtTime(current);
  document.getElementById('dur-time').textContent = fmtTime(duration);
}

function setupMediaSession(item) {
  if (!('mediaSession' in navigator)) return;
  try {
    navigator.mediaSession.metadata = new window.MediaMetadata({
      title: item.title,
      artist: state.student?.name || '英语磨耳朵',
      album: '磨耳朵',
    });
    navigator.mediaSession.setActionHandler('play', () => activeEl().play().catch(() => {}));
    navigator.mediaSession.setActionHandler('pause', () => activeEl().pause());
    navigator.mediaSession.setActionHandler('previoustrack', () => step(-1));
    navigator.mediaSession.setActionHandler('nexttrack', () => step(1));
  } catch (err) { /* 不支持就算了 */ }
}

/* ------------------------------------------------------------ 事件上报 */

function completionTarget(item) {
  return item.speech_ms && item.speech_ms > 0 ? item.speech_ms : (item.duration_ms || 0);
}

async function finalizeSession(session, item, completedByEnding) {
  if (!session || !item || session.finalized) return;
  session.finalized = true;

  const target = completionTarget(item);
  const ratio = target > 0 ? session.maxPosMs / target : 0;
  const completed = Boolean(completedByEnding) && ratio >= MIN_COMPLETE_RATIO
    && session.effectiveMs >= MIN_COMPLETE_MS;

  const event = {
    event_id: uuid(),
    video_id: session.videoId,
    started_at: session.startedAt,
    ended_at: new Date().toISOString(),
    effective_ms: session.effectiveMs,
    max_position_ms: session.maxPosMs,
    seek_count: session.seeks,
    avg_rate: activeEl().playbackRate || 1.0,
    completed,
    client_version: `student-web-0.3-${state.mode}`,
  };

  if (session.effectiveMs >= 1000) {
    state.queue.push(event);
    localStorage.setItem('event_queue', JSON.stringify(state.queue));
  }
  if (completed) {
    const target0 = (state.today?.items || []).find((i) => i.video_id === session.videoId);
    if (target0) target0.completed = true;
  }
  await flushQueue();
}

/** 结算"当前"这一段（返回前把 session 摘掉，避免重复结算）。 */
async function finalizeCurrent(completedByEnding) {
  const session = state.session;
  const item = currentItem();
  state.session = null;
  if (!session || !item) return;
  await finalizeSession(session, item, completedByEnding);
}

async function flushQueue() {
  state.queue = JSON.parse(localStorage.getItem('event_queue') || '[]');
  if (!state.queue.length) return;
  if (typeof navigator !== 'undefined' && navigator.onLine === false) return;
  try {
    await api('/api/events', {
      method: 'POST',
      body: JSON.stringify({ events: state.queue }),
    });
    state.queue = [];
    localStorage.removeItem('event_queue');
  } catch (err) { /* 留着下次再传 */ }
}

window.addEventListener('online', () => { flushQueue(); });
window.addEventListener('pagehide', () => { finalizeCurrent(false).catch(() => {}); });

/* 媒体加载失败时把原因显示出来，不要静默失败 */
[audio, video].forEach((el) => {
  el.addEventListener('error', async () => {
    if (el !== activeEl()) return;
    const code = el.error?.code;
    const reason = {
      1: '加载被中止',
      2: '网络错误',
      3: '解码失败（格式不支持）',
      4: '地址无效或没有权限',
    }[code] || '未知错误';
    const src = el.currentSrc || el.getAttribute('src') || '(空)';
    const shortSrc = src.replace(/token=[^&]+/, 'token=***').slice(0, 120);
    document.getElementById('player-tip').textContent =
      `播放失败：${reason}（${code}）· ${shortSrc}`;
    console.warn('[media error]', code, src);

    // 最常见的原因是设备令牌过期（换了数据库、撤销过设备、重新部署过）
    // 先探测令牌，能区分"地址/格式问题"和"该重新配对了"
    if (code === 4) {
      const stillValid = await tokenValid();
      if (!stillValid) {
        document.getElementById('repair-btn').classList.remove('hidden');
        document.getElementById('player-tip').textContent = '配对已失效，请让家长重新生成配对码';
        toast('配对已失效');
      } else {
        toast(`播放失败：${reason}`);
      }
    } else {
      toast(`播放失败：${reason}`);
    }
  });
  el.addEventListener('stalled', () => {
    if (el !== activeEl()) return;
    document.getElementById('player-tip').textContent = '网络有点慢，正在缓冲…';
  });
});

document.getElementById('repair-btn').addEventListener('click', () => {
  forgetToken('请让家长在「设置」里生成新的配对码');
});

/* ------------------------------------------------------------ 启动 */

(async function boot() {
  applyVolume();
  updateAutoLevelButton();
  renderMode();
  if (!state.token) {
    showScreen('pair');
    return;
  }
  await loadHome();
})();
