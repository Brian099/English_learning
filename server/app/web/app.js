'use strict';

const state = {
  token: localStorage.getItem('token') || '',
  tab: 'dashboard',
  students: [],
  series: [],
  seriesFilter: '',
  levelFilter: '',
  planStudentId: null,
  planDate: todayISO(),
  planSelected: new Set(),
  videosCache: [],
};

function todayISO() {
  const now = new Date();
  const off = now.getTimezoneOffset() * 60000;
  return new Date(now - off).toISOString().slice(0, 10);
}

function esc(value) {
  return String(value ?? '').replace(/[&<>"']/g, (ch) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]
  ));
}

function fmtDuration(ms) {
  if (!ms) return '—';
  const total = Math.round(ms / 1000);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${m}:${String(s).padStart(2, '0')}`;
}

function toast(message) {
  const el = document.getElementById('toast');
  el.textContent = message;
  el.classList.remove('hidden');
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => el.classList.add('hidden'), 2200);
}

async function api(path, options = {}) {
  const headers = Object.assign({ 'Content-Type': 'application/json' }, options.headers || {});
  if (state.token) headers.Authorization = `Bearer ${state.token}`;
  const res = await fetch(path, Object.assign({}, options, { headers }));
  if (res.status === 401) {
    logout();
    throw new Error('登录已过期，请重新登录');
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) throw new Error((data && data.detail) || res.statusText);
  return data;
}

/* ------------------------------------------------------------- 登录 */

function showLogin() {
  document.getElementById('login').classList.remove('hidden');
  document.getElementById('app').classList.add('hidden');
}

function showApp() {
  document.getElementById('login').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
  switchTab(state.tab);
}

function logout() {
  state.token = '';
  localStorage.removeItem('token');
  showLogin();
}

document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const password = document.getElementById('password').value;
  const errorEl = document.getElementById('login-error');
  errorEl.textContent = '';
  try {
    const data = await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ password }),
    });
    state.token = data.token;
    localStorage.setItem('token', data.token);
    showApp();
  } catch (err) {
    errorEl.textContent = err.message;
  }
});

document.getElementById('logout').addEventListener('click', logout);

/* ------------------------------------------------------------- 标签页 */

document.getElementById('tabs').addEventListener('click', (event) => {
  const button = event.target.closest('button[data-tab]');
  if (!button) return;
  switchTab(button.dataset.tab);
});

function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll('#tabs button').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  ['dashboard', 'library', 'plan', 'settings'].forEach((name) => {
    document.getElementById(`tab-${name}`).classList.toggle('hidden', name !== tab);
  });
  if (tab === 'dashboard') renderDashboard();
  if (tab === 'library') renderLibrary();
  if (tab === 'plan') renderPlan();
  if (tab === 'settings') renderSettings();
}

/* ------------------------------------------------------------- 概览 */

async function renderDashboard() {
  const root = document.getElementById('tab-dashboard');
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await api('/api/dashboard');
    if (!data.students.length) {
      root.innerHTML = '<div class="card"><div class="empty">还没有学生，去「设置」里添加一个</div></div>';
      return;
    }
    root.innerHTML = data.students.map(studentCard).join('');
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

function studentCard(card) {
  const today = card.today;
  const percent = today.target_min > 0
    ? Math.min(100, Math.round((today.effective_min / today.target_min) * 100))
    : 0;
  const done = today.effective_min >= today.target_min && today.target_min > 0;
  const maxMin = Math.max(1, ...card.week.days.map((d) => d.effective_min));

  const bars = card.week.days.map((day) => {
    const height = Math.max(3, Math.round((day.effective_min / maxMin) * 48));
    return `<div class="day" title="${day.day}：${day.effective_min} 分钟">
      <div class="col" style="height:48px"><i style="height:${height}px"></i></div>
      <span>${day.day.slice(5)}</span>
    </div>`;
  }).join('');

  return `<div class="card">
    <div class="spread">
      <h2>${esc(card.student.name)}</h2>
      <span class="badge">连续 ${card.streak} 天</span>
    </div>
    <div class="row" style="align-items:baseline">
      <span class="big">${today.effective_min}</span><span class="unit">/ ${today.target_min} 分钟</span>
      ${done ? '<span class="badge l1">已达标</span>' : ''}
    </div>
    <div class="bar ${done ? 'done' : ''}"><i style="width:${percent}%"></i></div>
    <div class="muted">今天完成 ${today.completed_count} 集 · 本周 ${card.week.total_min} 分钟 / ${card.week.active_days} 天有听</div>
    <div class="week">${bars}</div>
  </div>`;
}

/* ------------------------------------------------------------- 内容库 */

async function ensureSeries() {
  if (state.series.length) return state.series;
  state.series = await api('/api/series');
  return state.series;
}

async function renderLibrary() {
  const root = document.getElementById('tab-library');
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    await ensureSeries();
    const params = new URLSearchParams();
    if (state.seriesFilter) params.set('series_id', state.seriesFilter);
    if (state.levelFilter) params.set('level', state.levelFilter);
    const data = await api(`/api/videos?${params.toString()}`);
    state.videosCache = data.items;

    const seriesChips = [
      `<button class="chip ${state.seriesFilter === '' ? 'active' : ''}" data-series="">全部系列</button>`,
      ...state.series.map((s) => `<button class="chip ${String(state.seriesFilter) === String(s.id) ? 'active' : ''}"
        data-series="${s.id}">${esc(s.name)} (${s.video_count})</button>`),
    ].join('');

    const levelChips = ['', 'L1', 'L2', 'L3', 'L4', 'L5'].map((level) => (
      `<button class="chip ${state.levelFilter === level ? 'active' : ''}" data-level="${level}">
        ${level || '全部难度'}</button>`
    )).join('');

    const items = data.items.map((video) => {
      const level = video.level ? `<span class="badge ${video.level.toLowerCase()}">${video.level}</span>` : '';
      const speech = video.speech_pct ? `${Math.round(video.speech_pct)}% 语音` : '未转写';
      const wpm = video.wpm ? ` · ${Math.round(video.wpm)} WPM` : '';
      return `<div class="item" data-video="${video.id}">
        <div class="grow">
          <div class="title">${esc(video.title)}</div>
          <div class="meta">${fmtDuration(video.duration_ms)} · ${speech}${wpm}</div>
        </div>
        ${level}
      </div>`;
    }).join('') || '<div class="empty">没有符合条件的视频</div>';

    root.innerHTML = `
      <div class="card">
        <div class="chips">${seriesChips}</div>
        <div class="chips">${levelChips}</div>
        <div class="muted">共 ${data.total} 集</div>
      </div>
      <div class="card"><div class="list">${items}</div></div>`;

    root.querySelectorAll('[data-series]').forEach((el) => {
      el.addEventListener('click', () => {
        state.seriesFilter = el.dataset.series;
        renderLibrary();
      });
    });
    root.querySelectorAll('[data-level]').forEach((el) => {
      el.addEventListener('click', () => {
        state.levelFilter = el.dataset.level;
        renderLibrary();
      });
    });
    root.querySelectorAll('[data-video]').forEach((el) => {
      el.addEventListener('click', () => openVideo(el.dataset.video));
    });
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

/* ------------------------------------------------------------- 详情 */

async function openVideo(id) {
  const body = document.getElementById('modal-body');
  body.innerHTML = '<div class="empty">加载中…</div>';
  document.getElementById('modal').classList.remove('hidden');
  try {
    const video = await api(`/api/videos/${id}`);
    let transcript = null;
    try {
      transcript = await api(`/api/videos/${id}/transcript`);
    } catch (err) {
      transcript = null;
    }

    const rows = [
      ['时长', fmtDuration(video.duration_ms)],
      ['语音时长', fmtDuration(video.speech_ms) + (video.speech_pct ? `（${Math.round(video.speech_pct)}%）` : '')],
      ['语速', video.wpm ? `${Math.round(video.wpm)} WPM` : '—'],
      ['难度', video.level ? `${video.level}（生词率 ${Math.round((video.oov_ratio || 0) * 100)}%）` : '未分级'],
      ['唯一词', video.unique_words || '—'],
      ['分辨率', video.width ? `${video.width}×${video.height}` : '—'],
    ].map(([k, v]) => `<div class="spread" style="padding:5px 0;border-bottom:1px solid var(--line)">
        <span class="muted">${k}</span><span>${esc(v)}</span></div>`).join('');

    body.innerHTML = `
      <h2 style="margin:0 0 4px">${esc(video.title)}</h2>
      <div class="muted" style="margin-bottom:14px">${esc(video.series || '')}</div>
      ${rows}
      <h3 style="margin-top:18px">转写文本</h3>
      ${transcript
        ? `<div class="muted">${transcript.model} · ${transcript.words} 词</div>
           <div class="transcript">${esc(transcript.text)}</div>`
        : '<div class="empty">还没有转写结果</div>'}`;
  } catch (err) {
    body.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

document.getElementById('modal-close').addEventListener('click', () => {
  document.getElementById('modal').classList.add('hidden');
});
document.getElementById('modal').addEventListener('click', (event) => {
  if (event.target.id === 'modal') document.getElementById('modal').classList.add('hidden');
});

/* ------------------------------------------------------------- 计划 */

async function renderPlan() {
  const root = document.getElementById('tab-plan');
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    await ensureSeries();
    state.students = await api('/api/students');
    if (!state.students.length) {
      root.innerHTML = '<div class="card"><div class="empty">还没有学生，去「设置」里添加</div></div>';
      return;
    }
    if (!state.planStudentId) state.planStudentId = state.students[0].id;

    const studentOptions = state.students.map((s) => (
      `<option value="${s.id}" ${s.id === state.planStudentId ? 'selected' : ''}>${esc(s.name)}</option>`
    )).join('');

    root.innerHTML = `
      <div class="card">
        <div class="row">
          <select id="plan-student">${studentOptions}</select>
          <input type="date" id="plan-date" value="${state.planDate}">
          <label class="muted" style="display:flex;align-items:center;gap:6px">
            目标 <input type="number" id="plan-target" min="0" max="180" style="width:72px">
          </label>
        </div>
      </div>
      <div class="card">
        <h2>选择今天要听的</h2>
        <div class="chips">${state.series.map((s) => (
          `<button class="chip ${String(state.seriesFilter) === String(s.id) ? 'active' : ''}"
            data-plan-series="${s.id}">${esc(s.name)}</button>`
        )).join('')}</div>
        <div id="plan-list" class="list"><div class="empty">加载中…</div></div>
      </div>
      <div class="card">
        <div class="row">
          <button id="plan-auto">✨ 自动排片</button>
          <button id="plan-save" class="ghost">保存手动调整</button>
          <span class="muted" id="plan-count">已选 0 集</span>
          <span class="muted" id="plan-source"></span>
        </div>
      </div>`;

    document.getElementById('plan-student').addEventListener('change', (event) => {
      state.planStudentId = Number(event.target.value);
      loadPlan();
    });
    document.getElementById('plan-date').addEventListener('change', (event) => {
      state.planDate = event.target.value;
      loadPlan();
    });
    root.querySelectorAll('[data-plan-series]').forEach((el) => {
      el.addEventListener('click', () => {
        state.seriesFilter = state.seriesFilter === el.dataset.planSeries ? '' : el.dataset.planSeries;
        renderPlan();
      });
    });
    document.getElementById('plan-save').addEventListener('click', savePlan);
    document.getElementById('plan-auto').addEventListener('click', autoPlan);

    await loadPlan();
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

async function loadPlan() {
  const listEl = document.getElementById('plan-list');
  if (!listEl) return;
  listEl.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const params = new URLSearchParams();
    if (state.seriesFilter) params.set('series_id', state.seriesFilter);
    const data = await api(`/api/videos?${params.toString()}`);

    let existing = { items: [] };
    try {
      existing = await api(`/api/plans/${state.planStudentId}/${state.planDate}`);
    } catch (err) {
      existing = { items: [] };
    }
    state.planSelected = new Set((existing.items || []).map((item) => item.video_id));

    // 目标分钟数：优先用当天已有的计划，其次用学生的默认目标
    const targetInput = document.getElementById('plan-target');
    if (targetInput) {
      const student = state.students.find((s) => s.id === state.planStudentId);
      targetInput.value = existing.target_min ?? (student ? student.daily_target_min : 20);
    }

    const sourceEl = document.getElementById('plan-source');
    if (sourceEl) {
      sourceEl.textContent = existing.source === 'auto'
        ? '· 程序自动排的'
        : (existing.source === 'manual' ? '· 家长手动排的' : '');
    }

    listEl.innerHTML = data.items.map((video) => `
      <label class="item">
        <input type="checkbox" data-pick="${video.id}" ${state.planSelected.has(video.id) ? 'checked' : ''}>
        <div class="grow">
          <div class="title">${esc(video.title)}</div>
          <div class="meta">${fmtDuration(video.duration_ms)}${video.level ? ' · ' + video.level : ''}</div>
        </div>
      </label>`).join('') || '<div class="empty">没有视频</div>';

    listEl.querySelectorAll('[data-pick]').forEach((el) => {
      el.addEventListener('change', () => {
        const id = Number(el.dataset.pick);
        if (el.checked) state.planSelected.add(id);
        else state.planSelected.delete(id);
        updatePlanCount();
      });
    });
    updatePlanCount();
  } catch (err) {
    listEl.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
  }
}

function updatePlanCount() {
  const el = document.getElementById('plan-count');
  if (el) el.textContent = `已选 ${state.planSelected.size} 集`;
}

async function autoPlan() {
  const targetInput = document.getElementById('plan-target');
  const target = targetInput ? Number(targetInput.value) : 20;
  try {
    const query = Number.isFinite(target) && target > 0 ? `?target_min=${target}` : '';
    const result = await api(`/api/plans/${state.planStudentId}/${state.planDate}/auto${query}`, {
      method: 'POST',
    });
    if (result.message) {
      toast(result.message);
      await loadPlan();
      return;
    }
    toast(`已排 ${result.items} 集（复习 ${result.review_count} · 新内容 ${result.new_count}）`);
    await loadPlan();
  } catch (err) {
    toast(err.message);
  }
}

async function savePlan() {
  try {
    const targetInput = document.getElementById('plan-target');
    const targetMin = targetInput ? Number(targetInput.value) : 20;
    await api(`/api/plans/${state.planStudentId}/${state.planDate}`, {
      method: 'PUT',
      body: JSON.stringify({
        target_min: Number.isFinite(targetMin) && targetMin >= 0 ? targetMin : 20,
        note: null,
        video_ids: [...state.planSelected],
      }),
    });
    toast('计划已保存');
  } catch (err) {
    toast(err.message);
  }
}

/* ------------------------------------------------------------- 设置 */

async function renderSettings() {
  const root = document.getElementById('tab-settings');
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    state.students = await api('/api/students');
    const devices = await api('/api/devices');

    const studentRows = state.students.map((s) => `
      <div class="item">
        <div class="grow"><div class="title">${esc(s.name)}</div>
          <div class="meta">每日目标 ${s.daily_target_min} 分钟</div></div>
        <button class="ghost small" data-pair="${s.id}">配对码</button>
      </div>`).join('') || '<div class="empty">还没有学生</div>';

    const deviceRows = devices.map((d) => `
      <div class="item">
        <div class="grow"><div class="title">${esc(d.name)}</div>
          <div class="meta">${esc(d.platform || '未知平台')} · 最后活跃 ${d.last_seen_at ? d.last_seen_at.slice(0, 16).replace('T', ' ') : '—'}</div></div>
        <button class="ghost small" data-revoke="${d.id}">撤销</button>
      </div>`).join('') || '<div class="empty">还没有配对设备</div>';

    root.innerHTML = `
      <div class="card">
        <h2>学生</h2>
        <div class="list">${studentRows}</div>
        <div class="row" style="margin-top:12px">
          <input type="text" id="new-student" placeholder="学生姓名">
          <button id="add-student">添加</button>
        </div>
      </div>
      <div class="card">
        <h2>设备</h2>
        <div class="list">${deviceRows}</div>
        <div class="muted" style="margin-top:10px">在平板上输入 6 位配对码即可绑定</div>
      </div>
      <div class="card">
        <h2>内容维护</h2>
        <div class="row">
          <button id="do-scan">扫描视频目录</button>
          <button id="do-import" class="hidden">导入并处理</button>
          <button id="do-audio" class="ghost">补跑入库（失败重试）</button>
        </div>
        <div id="scan-result" class="muted" style="margin-top:10px;line-height:1.7"></div>
        <span class="muted" id="maintenance-status"></span>
        <p class="muted" style="margin:10px 0 0">
          扫描只做检测，不写库、不处理。确认数字后再点「导入并处理」。
          文件名请自己整理（第一层文件夹名 = 系列名），程序不改名。
        </p>
      </div>
      <div class="card">
        <h2>学生端</h2>
        <p class="muted" style="margin:0 0 10px">
          给孩子用的页面：配对后只听不看，熄屏也能继续播。
        </p>
        <a href="/student.html" target="_blank" rel="noopener"
           style="display:inline-block;padding:10px 16px;border-radius:10px;
                  background:var(--brand);color:#fff;text-decoration:none;font-size:14px">
          打开学生端 →
        </a>
      </div>`;

    document.getElementById('add-student').addEventListener('click', async () => {
      const input = document.getElementById('new-student');
      const name = input.value.trim();
      if (!name) return;
      try {
        await api('/api/students', { method: 'POST', body: JSON.stringify({ name }) });
        toast('已添加');
        renderSettings();
      } catch (err) { toast(err.message); }
    });

    root.querySelectorAll('[data-pair]').forEach((el) => {
      el.addEventListener('click', async () => {
        try {
          const data = await api('/api/devices/pair-code', {
            method: 'POST',
            body: JSON.stringify({ student_id: Number(el.dataset.pair), device_name: '平板' }),
          });
          showModal(`<h2>配对码</h2>
            <div class="big" style="letter-spacing:6px;margin:16px 0">${esc(data.code)}</div>
            <div class="muted">5 分钟内有效，在平板上输入即可</div>`);
        } catch (err) { toast(err.message); }
      });
    });

    root.querySelectorAll('[data-revoke]').forEach((el) => {
      el.addEventListener('click', async () => {
        if (!confirm('确定撤销该设备？')) return;
        try {
          await api(`/api/devices/${el.dataset.revoke}`, { method: 'DELETE' });
          toast('已撤销');
          renderSettings();
        } catch (err) { toast(err.message); }
      });
    });

    document.getElementById('do-scan').addEventListener('click', async () => {
      const resultEl = document.getElementById('scan-result');
      const importBtn = document.getElementById('do-import');
      resultEl.textContent = '正在检测…';
      importBtn.classList.add('hidden');
      try {
        const data = await api('/api/scan/preview', { method: 'POST' });
        renderScanResult(data);
      } catch (err) {
        resultEl.textContent = err.message;
      }
    });

    document.getElementById('do-import').addEventListener('click', async () => {
      const resultEl = document.getElementById('scan-result');
      const importBtn = document.getElementById('do-import');
      try {
        const result = await api('/api/scan', { method: 'POST' });
        toast(`已导入 新增 ${result.added || 0}，更新 ${result.updated || 0}` +
              (result.ingest_started ? '，开始处理' : ''));
        importBtn.classList.add('hidden');
        resultEl.textContent = `已导入：新增 ${result.added || 0} · 更新 ${result.updated || 0}`;
        if (result.ingest_started) watchIngest(result.ingest_count);
        else await renderSettings();
      } catch (err) {
        toast(err.message);
      }
    });

    document.getElementById('do-audio').addEventListener('click', startAudioExtraction);
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

function showModal(html) {
  document.getElementById('modal-body').innerHTML = html;
  document.getElementById('modal').classList.remove('hidden');
}

/* 扫描检测结果 */
function renderScanResult(data) {
  const el = document.getElementById('scan-result');
  const importBtn = document.getElementById('do-import');

  if (data.error) {
    el.textContent = data.error;
    importBtn.classList.add('hidden');
    return;
  }

  const lines = [];
  lines.push(`媒体目录：${esc(data.media_root)}`);
  lines.push(`共 <b>${data.total}</b> 个视频文件：新增 <b>${data.new}</b> · ` +
             `变化 <b>${data.changed}</b> · 未变 ${data.unchanged}`);

  const series = Object.entries(data.series || {});
  if (series.length) {
    lines.push('按系列：' + series.map(([name, s]) => {
      const extra = s.new || s.changed ? `（新 ${s.new}${s.changed ? ` / 变 ${s.changed}` : ''}）` : '';
      return `${esc(name)} ${s.total}${extra}`;
    }).join(' · '));
  }

  if (data.pending > 0) {
    lines.push(`预计处理 <b>${data.pending}</b> 集，约 <b>${data.estimated_minutes}</b> 分钟`);
    if (data.new_examples && data.new_examples.length) {
      lines.push('新增示例：' + data.new_examples.map(esc).join('、'));
    }
    importBtn.textContent = `导入并处理 ${data.pending} 个`;
    importBtn.classList.remove('hidden');
  } else {
    lines.push('没有新增或变化的文件，无需导入。');
    importBtn.classList.add('hidden');
  }

  el.innerHTML = lines.join('<br>');
}
/* 补跑入库（失败重试）：后台任务 + 轮询进度 */
async function startAudioExtraction() {
  try {
    const started = await api('/api/maintenance/ingest', { method: 'POST' });
    if (!started.started) {
      toast(started.reason || '没有需要处理的视频');
      return;
    }
    toast(`开始处理 ${started.pending} 集（模型 ${started.model}）`);
    watchIngest(started.pending);
  } catch (err) {
    toast(err.message);
  }
}

let ingestTimer = null;

function watchIngest(expected) {
  const statusEl = document.getElementById('maintenance-status');
  if (ingestTimer) clearInterval(ingestTimer);
  if (statusEl) statusEl.textContent = `处理中 0/${expected || '?'}`;

  ingestTimer = setInterval(async () => {
    try {
      const s = await api('/api/maintenance/status');
      if (statusEl) {
        statusEl.textContent = s.running
          ? `${s.stage || '处理中'} ${s.done}/${s.total}${s.failed ? `（失败 ${s.failed}）` : ''}`
          : `已完成 ${s.done}/${s.total}${s.failed ? `，失败 ${s.failed}` : ''}`;
      }
      if (!s.running) {
        clearInterval(ingestTimer);
        ingestTimer = null;
        toast(s.failed ? `处理完成，${s.failed} 集失败` : '全部处理完成');
      }
    } catch (err) {
      clearInterval(ingestTimer);
      ingestTimer = null;
    }
  }, 2000);
}

/* ------------------------------------------------------------- 启动 */

(async function boot() {
  if (!state.token) {
    showLogin();
    return;
  }
  try {
    await api('/api/students');
    showApp();
  } catch (err) {
    showLogin();
  }
})();
