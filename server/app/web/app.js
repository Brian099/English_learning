'use strict';

const state = {
  token: localStorage.getItem('token') || '',
  role: localStorage.getItem('role') || '',          // owner | member
  family: localStorage.getItem('family') || '',
  username: localStorage.getItem('username') || '',
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
  applyRole();
  switchTab(state.tab);
}

function logout() {
  state.token = '';
  state.role = '';
  ['token', 'role', 'family', 'username'].forEach((key) => localStorage.removeItem(key));
  showLogin();
}

/** 按角色显示/隐藏标签页 */
function applyRole() {
  document.querySelectorAll('#tabs button[data-tab]').forEach((button) => {
    const roles = (button.dataset.roles || '').split(/\s+/).filter(Boolean);
    const allowed = !roles.length || roles.includes(state.role);
    button.classList.toggle('hidden', !allowed);
  });
  const who = document.getElementById('who');
  if (who) {
    who.textContent = state.role
      ? `${state.family || ''} · ${state.role === 'owner' ? '主账号' : '子账号'}`
      : '';
  }
  const first = [...document.querySelectorAll('#tabs button[data-tab]')]
    .find((b) => !b.classList.contains('hidden'));
  if (first && !document.querySelector(`#tabs button[data-tab="${state.tab}"]:not(.hidden)`)) {
    state.tab = first.dataset.tab;
  }
}

document.getElementById('login-form').addEventListener('submit', async (event) => {
  event.preventDefault();
  const username = document.getElementById('username').value.trim() || 'parent';
  const password = document.getElementById('password').value;
  const errorEl = document.getElementById('login-error');
  errorEl.textContent = '';
  try {
    const data = await api('/api/admin/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    });
    state.token = data.token;
    state.role = data.role || '';
    state.family = data.family || '';
    state.username = data.username || username;
    localStorage.setItem('token', data.token);
    localStorage.setItem('role', state.role);
    localStorage.setItem('family', state.family);
    localStorage.setItem('username', state.username);
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
  ['dashboard', 'library', 'plan', 'accounts', 'settings'].forEach((name) => {
    document.getElementById(`tab-${name}`).classList.toggle('hidden', name !== tab);
  });
  if (tab === 'dashboard') renderDashboard();
  if (tab === 'library') renderLibrary();
  if (tab === 'plan') renderPlan();
  if (tab === 'accounts') renderAccounts();
  if (tab === 'settings') renderSettings();
}

function fmtBytes(bytes) {
  if (!bytes) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB'];
  let value = bytes;
  let index = 0;
  while (value >= 1024 && index < units.length - 1) { value /= 1024; index += 1; }
  return `${value.toFixed(value >= 100 ? 0 : 1)} ${units[index]}`;
}

/** 主账号概览：资源与家庭统计 */
async function renderOwnerOverview(root) {
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const data = await api('/api/overview');
    const f = data.family;
    const c = data.content;
    const cache = data.cache;
    const ingest = data.ingest;

    const ingestBar = ingest.running
      ? `<div class="muted" style="margin-top:8px">
           正在处理：${ingest.done}/${ingest.total}（${esc(ingest.stage)}）
         </div>`
      : `<div class="muted" style="margin-top:8px">当前空闲${ingest.pending ? `，还有 ${ingest.pending} 集待处理` : ''}</div>`;

    root.innerHTML = `
      <div class="card">
        <h2>家庭与账号</h2>
        <div class="stats">
          <div class="stat"><b>${f.count}</b><span>家庭</span></div>
          <div class="stat"><b>${f.accounts}</b><span>账号（主 ${f.owners} / 子 ${f.members}）</span></div>
          <div class="stat"><b>${f.students}</b><span>学生</span></div>
        </div>
      </div>

      <div class="card">
        <h2>内容资源</h2>
        <div class="stats">
          <div class="stat"><b>${c.videos}</b><span>视频集数</span></div>
          <div class="stat"><b>${c.series}</b><span>系列</span></div>
          <div class="stat"><b>${c.total_minutes}</b><span>总时长（分钟）</span></div>
          <div class="stat"><b>${fmtBytes(c.size_bytes)}</b><span>视频占用</span></div>
        </div>
      </div>

      <div class="card">
        <h2>入库状态</h2>
        <div class="stats">
          <div class="stat"><b>${c.transcripts}</b><span>已提取对白</span></div>
          <div class="stat"><b>${c.graded}</b><span>已分级</span></div>
          <div class="stat"><b>${c.with_audio}</b><span>有独立音轨</span></div>
          <div class="stat"><b>${c.pending}</b><span>待处理</span></div>
        </div>
        ${ingestBar}
        <div class="muted" style="margin-top:6px">转写模型：${esc(ingest.model)}</div>
        ${ingest.last_error ? `<div class="error" style="margin-top:6px">${esc(ingest.last_error)}</div>` : ''}
      </div>

      <div class="card">
        <h2>缓存占用</h2>
        <div class="stats">
          <div class="stat"><b>${fmtBytes(cache.audio_bytes)}</b><span>播放音轨</span></div>
          <div class="stat"><b>${fmtBytes(cache.asr_bytes)}</b><span>转写输入</span></div>
          <div class="stat"><b>${fmtBytes(cache.total_bytes)}</b><span>合计</span></div>
        </div>
        <div class="muted" style="margin-top:8px">
          媒体目录 ${esc(data.paths.media_root)} · 缓存目录 ${esc(data.paths.cache_root)}
        </div>
      </div>`;
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

/* ------------------------------------------------------------- 概览 */

async function renderDashboard() {
  const root = document.getElementById('tab-dashboard');
  if (state.role === 'owner') return renderOwnerOverview(root);
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
    <div class="toolbar" style="align-items:baseline">
      <span class="fs-3 fw-bold">${today.effective_min}</span><span class="unit">/ ${today.target_min} 分钟</span>
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
        <div class="toolbar">
          <select id="plan-student" class="form-select" style="width:auto;min-width:140px">${studentOptions}</select>
          <button id="plan-auto" class="btn btn-outline-secondary">✨ 重排今日</button>
          <span class="muted" id="plan-hint"></span>
        </div>
        <div class="muted" style="margin-top:8px;line-height:1.6">
          排片按星期目标自动生成，已听过的不会再排。
          目标在「设置 → 学生 → 编辑」里按周一到周日分别设置。
        </div>
      </div>
      <div id="week-view"></div>`;

    document.getElementById('plan-student').addEventListener('change', (event) => {
      state.planStudentId = Number(event.target.value);
      loadPlan();
    });
    document.getElementById('plan-auto').addEventListener('click', autoPlan);

    await loadPlan();
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

/** 未来 7 天的排片视图 */
async function loadPlan() {
  const root = document.getElementById('week-view');
  if (!root) return;
  root.innerHTML = '<div class="card"><div class="empty">加载中…</div></div>';
  try {
    const data = await api(`/api/schedule/${state.planStudentId}?days=7`);
    const hint = document.getElementById('plan-hint');
    if (hint) {
      const targets = (data.weekday_targets || []).join(' / ');
      hint.textContent = targets ? `周一→周日 ${targets} 分钟` : '';
    }

    root.innerHTML = data.days.map((day) => {
      const badge = day.is_today
        ? '<span class="chip active" style="font-size:12px;padding:2px 8px">今天</span>'
        : '';
      const short = day.short ? ' · <span style="color:#d9453b">内容不足</span>' : '';
      const items = day.items.length
        ? day.items.map((item, index) => `
            <div class="item" style="padding:8px 0;border-bottom:1px dashed #eef2ff">
              <div class="grow">
                <div class="title" style="font-size:14px">${index + 1}. ${esc(item.title)}</div>
              </div>
              <span class="muted">${fmtDuration(item.duration_ms)}</span>
            </div>`).join('')
        : '<div class="empty" style="padding:10px 0">没有可排的内容（视频都听过了）</div>';

      return `
        <div class="card">
          <div class="toolbar" style="justify-content:space-between;align-items:center">
            <div><b>${day.weekday}</b> <span class="muted">${day.day}</span> ${badge}</div>
            <div class="muted">目标 ${day.target_min} 分钟 · 实际 ${day.total_min} 分钟${short}</div>
          </div>
          <div class="list" style="margin-top:6px">${items}</div>
        </div>`;
    }).join('');
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

async function autoPlan() {
  const day = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' });
  try {
    const result = await api(`/api/plans/${state.planStudentId}/${day}/auto`, { method: 'POST' });
    toast(result.items
      ? `已重排 ${result.items} 集 · 目标 ${result.target_min} 分钟` +
        (result.message ? `（${result.message}）` : '')
      : (result.message || '没有可排的内容（视频都听过了）'));
    await loadPlan();
  } catch (err) {
    toast(err.message);
  }
}

/* ------------------------------------------------------------- 账号（主账号） */

async function renderAccounts() {
  const root = document.getElementById('tab-accounts');
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    const accounts = await api('/api/accounts');
    const rows = accounts.map((a) => `
      <div class="item">
        <div class="grow">
          <div class="title">${esc(a.display_name || a.username)}
            <span class="muted small">${a.role === 'owner' ? '主账号' : '子账号'}</span></div>
          <div class="meta">用户名 ${esc(a.username)}${a.active ? '' : ' · 已停用'}</div>
        </div>
        ${a.role === 'owner' ? '' : `
          <button class="btn btn-sm btn-outline-secondary" data-reset="${a.id}" data-name="${esc(a.username)}">改密码</button>
          <button class="btn btn-sm btn-outline-secondary" data-del="${a.id}">删除</button>`}
      </div>`).join('') || '<div class="empty">还没有子账号</div>';

    root.innerHTML = `
      <div class="card">
        <h2>家庭</h2>
        <div class="toolbar">
          <input type="text" id="family-name" class="form-control" value="${esc(state.family)}" placeholder="家庭名称">
          <button id="save-family" class="btn btn-primary">保存</button>
        </div>
        <p class="muted" style="margin:10px 0 0">
          一个账号对应一个家庭。主账号负责家庭设置、子账号和内容维护；
          子账号负责学生和计划。
        </p>
      </div>
      <div class="card">
        <h2>账号</h2>
        <div class="list">${rows}</div>
        <div class="toolbar" style="margin-top:12px">
          <input type="text" id="new-username" class="form-control" placeholder="子账号用户名">
          <input type="password" id="new-password" class="form-control" placeholder="密码（≥6 位）">
          <button id="add-account" class="btn btn-primary">添加子账号</button>
        </div>
      </div>`;

    document.getElementById('save-family').addEventListener('click', async () => {
      const name = document.getElementById('family-name').value.trim();
      if (!name) return;
      try {
        const saved = await api('/api/family', {
          method: 'PATCH', body: JSON.stringify({ name }),
        });
        state.family = saved.name;
        localStorage.setItem('family', saved.name);
        applyRole();
        toast('已保存');
      } catch (err) { toast(err.message); }
    });

    document.getElementById('add-account').addEventListener('click', async () => {
      const username = document.getElementById('new-username').value.trim();
      const password = document.getElementById('new-password').value;
      if (!username || password.length < 6) { toast('用户名必填，密码至少 6 位'); return; }
      try {
        await api('/api/accounts', {
          method: 'POST', body: JSON.stringify({ username, password }),
        });
        toast('已添加子账号');
        renderAccounts();
      } catch (err) { toast(err.message); }
    });

    root.querySelectorAll('[data-reset]').forEach((el) => {
      el.addEventListener('click', async () => {
        const pwd = prompt(`给「${el.dataset.name}」设置新密码（至少 6 位）`);
        if (!pwd) return;
        try {
          await api(`/api/accounts/${el.dataset.reset}`, {
            method: 'PATCH', body: JSON.stringify({ password: pwd }),
          });
          toast('密码已更新');
        } catch (err) { toast(err.message); }
      });
    });

    root.querySelectorAll('[data-del]').forEach((el) => {
      el.addEventListener('click', async () => {
        if (!confirm('确定删除这个子账号？')) return;
        try {
          await api(`/api/accounts/${el.dataset.del}`, { method: 'DELETE' });
          toast('已删除');
          renderAccounts();
        } catch (err) { toast(err.message); }
      });
    });
  } catch (err) {
    root.innerHTML = `<div class="card"><div class="empty">${esc(err.message)}</div></div>`;
  }
}

/* ------------------------------------------------------------- 设置 */

async function renderOwnerSettings(root) {
  root.innerHTML = `
    <div class="card">
      <h2>内容维护</h2>
      <div class="toolbar">
        <button id="do-scan" class="btn btn-primary">扫描视频目录</button>
        <button id="do-import" class="btn btn-primary hidden">导入并处理</button>
        <button id="do-audio" class="btn btn-outline-secondary">补跑入库（失败重试）</button>
      </div>
      <div id="scan-result" class="muted" style="margin-top:10px;line-height:1.7"></div>
      <span class="muted" id="maintenance-status"></span>
      <p class="muted" style="margin:10px 0 0">
        扫描只做检测，不写库、不处理。确认数字后再点「导入并处理」。
        文件名请自己整理（第一层文件夹名 = 系列名），程序不改名。
      </p>
    </div>`;

  wireMaintenance();
}

/** 内容维护按钮接线（主账号设置页用） */
function wireMaintenance() {
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
      const parts = [`新增 ${result.added || 0}`, `更新 ${result.updated || 0}`];
      if (result.removed) parts.push(`清理 ${result.removed}`);
      toast(parts.join('，') + (result.ingest_started ? '，开始处理' : ''));
      importBtn.classList.add('hidden');
      resultEl.textContent = parts.join(' · ') + (result.warning ? `　⚠ ${result.warning}` : '');
      if (result.warning) resultEl.textContent = `⚠ ${result.warning}`;
      if (result.ingest_started) watchIngest(result.ingest_count);
    } catch (err) {
      toast(err.message);
    }
  });

  document.getElementById('do-audio').addEventListener('click', startAudioExtraction);
}

async function renderSettings() {
  const root = document.getElementById('tab-settings');
  if (state.role === 'owner') return renderOwnerSettings(root);
  root.innerHTML = '<div class="empty">加载中…</div>';
  try {
    state.students = await api('/api/students');
    const devices = await api('/api/devices');

    const weekdayNames = ['一', '二', '三', '四', '五', '六', '日'];

    const studentRows = state.students.map((s) => {
      const targets = s.weekday_targets || Array(7).fill(s.daily_target_min);
      const summary = `周一~周日：${targets.join(' / ')} 分钟`;
      const weekdayInputs = weekdayNames.map((label, index) => `
        <label class="muted" style="display:flex;align-items:center;gap:3px;font-size:12px">
          ${label}<input type="number" data-weekday="${index}" class="form-control form-control-sm weekday-input" min="5" max="120" step="5" value="${targets[index] ?? s.daily_target_min}">
        </label>`).join('');

      return `
      <div class="item" data-student-row="${s.id}" style="align-items:flex-start">
        <div class="grow" data-view>
          <div class="title">${esc(s.name)}</div>
          <div class="meta">${summary}</div>
        </div>
        <div class="grow hidden" data-edit style="width:100%">
          <input type="text" data-edit-name class="form-control form-control-sm" value="${esc(s.name)}" placeholder="姓名" style="max-width:180px">
          <div style="display:flex;gap:8px;flex-wrap:wrap">${weekdayInputs}</div>
          <div class="muted" style="font-size:12px;margin-top:6px">
            周一到周日各自的目标分钟数；留空的那天用平均值
          </div>
        </div>
        <button class="btn btn-sm btn-primary" data-edit-save="${s.id}" hidden>保存</button>
        <button class="btn btn-sm btn-outline-secondary" data-edit-btn="${s.id}">编辑</button>
        <button class="btn btn-sm btn-outline-secondary" data-pair="${s.id}">配对码</button>
      </div>`;
    }).join('') || '<div class="empty">还没有学生</div>';

    const deviceRows = devices.map((d) => `
      <div class="item">
        <div class="grow"><div class="title">${esc(d.name)}</div>
          <div class="meta">${esc(d.platform || '未知平台')} · 最后活跃 ${d.last_seen_at ? d.last_seen_at.slice(0, 16).replace('T', ' ') : '—'}</div></div>
        <button class="btn btn-sm btn-outline-secondary" data-revoke="${d.id}">撤销</button>
      </div>`).join('') || '<div class="empty">还没有配对设备</div>';

    root.innerHTML = `
      <div class="card">
        <h2>学生</h2>
        <div class="list">${studentRows}</div>
        <div class="toolbar" style="margin-top:12px">
          <input type="text" id="new-student" class="form-control" placeholder="学生姓名">
          <button id="add-student" class="btn btn-primary">添加</button>
        </div>
      </div>
      <div class="card">
        <h2>设备</h2>
        <div class="list">${deviceRows}</div>
        <div class="muted" style="margin-top:10px">在平板上输入 6 位配对码即可绑定</div>
      </div>
      <div class="card">
        <h2>学生端</h2>
        <p class="muted" style="margin:0 0 10px">
          给孩子用的页面：配对后只听不看，熄屏也能继续播。
        </p>
        <a href="/student.html" target="_blank" rel="noopener" class="btn btn-primary">打开学生端 →</a>
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

    root.querySelectorAll('[data-edit-btn]').forEach((el) => {
      el.addEventListener('click', () => {
        const row = root.querySelector(`[data-student-row="${el.dataset.editBtn}"]`);
        if (!row) return;
        const editing = !row.querySelector('[data-edit]').classList.contains('hidden');
        row.querySelector('[data-view]').classList.toggle('hidden', !editing);
        row.querySelector('[data-edit]').classList.toggle('hidden', editing);
        row.querySelector('[data-edit-save]').hidden = editing;
        el.textContent = editing ? '编辑' : '取消';
      });
    });

    root.querySelectorAll('[data-edit-save]').forEach((el) => {
      el.addEventListener('click', async () => {
        const row = root.querySelector(`[data-student-row="${el.dataset.editSave}"]`);
        if (!row) return;
        const name = row.querySelector('[data-edit-name]').value.trim();
        if (!name) { toast('姓名不能为空'); return; }

        const targets = [...row.querySelectorAll('[data-weekday]')].map((input) => {
          const value = Number(input.value);
          return Number.isFinite(value) && value > 0 ? value : null;
        });
        const filled = targets.filter((value) => value !== null);
        if (!filled.length) { toast('至少填一天的目标时长'); return; }
        if (filled.some((value) => value < 5 || value > 120)) {
          toast('目标时长请填 5–120 分钟');
          return;
        }
        const average = Math.round(filled.reduce((sum, v) => sum + v, 0) / filled.length);
        const weekdayTargets = targets.map((value) => (value === null ? average : value));

        try {
          const saved = await api(`/api/students/${el.dataset.editSave}`, {
            method: 'PATCH',
            body: JSON.stringify({
              name,
              daily_target_min: average,
              weekday_targets: weekdayTargets,
            }),
          });
          const summary = (saved.weekday_targets || weekdayTargets).join('/');
          toast(`已保存：周一~周日 ${summary} 分钟` +
                (saved.plan_rebuilt ? '，今日计划已重排' : ''));
          await renderSettings();
        } catch (err) { toast(err.message); }
      });
    });

    root.querySelectorAll('[data-pair]').forEach((el) => {
      el.addEventListener('click', async () => {
        try {
          const data = await api('/api/devices/pair-code', {
            method: 'POST',
            body: JSON.stringify({ student_id: Number(el.dataset.pair), device_name: '平板' }),
          });
          showModal(`<h2>配对码</h2>
            <div class="fs-3 fw-bold" style="letter-spacing:6px;margin:16px 0">${esc(data.code)}</div>
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

  if (data.missing > 0) {
    lines.push(`<b style="color:#c0392b">缺失 ${data.missing} 个</b>：库里有记录但磁盘上找不到，导入时会彻底清理`);
    if (data.missing_examples && data.missing_examples.length) {
      lines.push('　例如：' + data.missing_examples.map((m) => esc(m.title)).join('、'));
    }
  }

  if (data.pending > 0) {
    lines.push(`预计处理 <b>${data.pending}</b> 集，约 <b>${data.estimated_minutes}</b> 分钟`);
    if (data.new_examples && data.new_examples.length) {
      lines.push('新增示例：' + data.new_examples.map(esc).join('、'));
    }
    importBtn.textContent = data.missing > 0
      ? `导入 ${data.pending} 个 · 清理 ${data.missing} 个`
      : `导入并处理 ${data.pending} 个`;
    importBtn.classList.remove('hidden');
  } else if (data.missing > 0) {
    importBtn.textContent = `清理 ${data.missing} 个缺失记录`;
    importBtn.classList.remove('hidden');
  } else {
    lines.push('没有新增、变化或缺失的文件，无需导入。');
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
    // 用 /api/account/me 探测会话：主账号和子账号都能访问
    // （之前用 /api/students，主账号会 403，导致每次刷新都掉回登录页）
    const me = await api('/api/account/me');
    state.role = me.role;
    state.family = me.family;
    state.username = me.username;
    localStorage.setItem('role', me.role);
    localStorage.setItem('family', me.family);
    localStorage.setItem('username', me.username);
    showApp();
  } catch (err) {
    showLogin();
  }
})();