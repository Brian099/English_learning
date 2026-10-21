'use strict';

/* 学生端：配对 → 今日任务 → 播放器（听/看双模式 + 硬字幕遮挡 + 有效时长统计） */

const state = {
  token: localStorage.getItem('student_token') || '',
  student: null,
  today: null,
  index: 0,
  loop: false,
  rate: 1.0,
  mode: localStorage.getItem('player_mode') || 'audio',   // audio | video
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

function showScreen(name) {
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
    list.innerHTML = '<div class="task"><div class="info"><div class="name">今天还没有任务</div></div></div>';
    return;
  }
  list.innerHTML = data.items.map((item, index) => `
    <div class="task ${item.completed ? 'done' : ''}" data-index="${index}">
      <div class="idx">${item.completed ? '✓' : index + 1}</div>
      <div class="info">
        <div class="name">${escapeHtml(item.title)}</div>
        <div class="meta">${fmtTime((item.duration_ms || 0) / 1000)}${item.level ? ' · ' + item.level : ''}</div>
      </div>
      <div class="idx" style="background:transparent;color:var(--brand)">▶</div>
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
    ? '看模式下熄屏会暂停，想一直听就切回「听」'
    : '熄屏也能继续听哦';
  applyMask();
}

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

  audio.pause();
  video.pause();
  audio.removeAttribute('src');
  video.removeAttribute('src');

  const el = activeEl();
  el.src = sourceFor(item);
  el.playbackRate = state.rate;
  el.loop = state.loop;

  renderMode();
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
  });
  el.addEventListener('pause', () => {
    if (el !== activeEl()) return;
    document.getElementById('btn-play').textContent = '▶';
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
  renderMode();
  if (!state.token) {
    showScreen('pair');
    return;
  }
  await loadHome();
})();
