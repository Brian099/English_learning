// 学生端网页的 jsdom 测试：配对 → 今日任务 → 抖音式播放器 → 有效时长 → 记录
// 用法：node tools/ui_test_student.mjs [--base http://127.0.0.1:8010] [--password dev]

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const require = createRequire(import.meta.url);
const { JSDOM } = require('../.ui-test/node_modules/jsdom');

const HERE = dirname(fileURLToPath(import.meta.url));
const PROJECT = join(HERE, '..');

const args = process.argv.slice(2);
const getArg = (name, fallback) => {
  const index = args.indexOf(`--${name}`);
  return index >= 0 && args[index + 1] ? args[index + 1] : fallback;
};
const BASE = getArg('base', 'http://127.0.0.1:8010');
const PASSWORD = getArg('password', 'dev');

let pass = 0;
let fail = 0;

function check(name, ok, detail = '') {
  if (ok) {
    pass += 1;
    console.log(`  ✓ ${name}${detail ? ' — ' + detail : ''}`);
  } else {
    fail += 1;
    console.log(`  ✗ ${name}${detail ? ' — ' + detail : ''}`);
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function waitFor(fn, { timeout = 8000, interval = 60 } = {}) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    try {
      const value = await fn();
      if (value) return value;
    } catch (err) { /* 继续等 */ }
    await sleep(interval);
  }
  return null;
}

const textOf = (el) => (el?.textContent || '').replace(/\s+/g, ' ').trim();

async function apiAs(token, path, method = 'GET', body = null) {
  const res = await fetch(BASE + path, {
    method,
    headers: Object.assign({ 'Content-Type': 'application/json' },
      token ? { Authorization: `Bearer ${token}` } : {}),
    body: body ? JSON.stringify(body) : undefined,
  });
  const text = await res.text();
  if (!res.ok) throw new Error(`${method} ${path} -> ${res.status} ${text.slice(0, 200)}`);
  return text ? JSON.parse(text) : null;
}

async function main() {
  const ownerToken = (await apiAs(null, '/api/admin/login', 'POST',
    { username: 'parent', password: PASSWORD })).token;
  // 学生和计划只能子账号管，先确保子账号存在
  let memberToken;
  try {
    memberToken = (await apiAs(null, '/api/admin/login', 'POST',
      { username: 'ui-member', password: 'ui-member-123' })).token;
  } catch (err) {
    await apiAs(ownerToken, '/api/accounts', 'POST',
      { username: 'ui-member', password: 'ui-member-123' });
    memberToken = (await apiAs(null, '/api/admin/login', 'POST',
      { username: 'ui-member', password: 'ui-member-123' })).token;
  }
  const students = await apiAs(memberToken, '/api/students');
  if (!students.length) throw new Error('没有学生，请先建一个');
  const studentId = students[0].id;

  // 保证今日计划至少 2 集，否则"上下滑切换"没法验证
  const day = new Date().toLocaleDateString('en-CA', { timeZone: 'Asia/Shanghai' });
  let plan = null;
  try { plan = await apiAs(memberToken, `/api/plans/${studentId}/${day}`); } catch (err) { plan = null; }
  if (!plan || (plan.items || []).length < 2) {
    await apiAs(memberToken, `/api/plans/${studentId}/${day}/auto?target_min=20`, 'POST');
  }

  const pairCode = (await apiAs(memberToken, '/api/devices/pair-code', 'POST',
    { student_id: studentId, device_name: '学生网页端测试' })).code;

  const html = readFileSync(join(PROJECT, 'server/app/web/student.html'), 'utf8');
  const appJs = readFileSync(join(PROJECT, 'server/app/web/student.js'), 'utf8');

  const dom = new JSDOM(html, { url: BASE + '/student.html', runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  const doc = window.document;

  window.fetch = (input, init) => fetch(new URL(input, BASE), init);
  window.confirm = () => true;
  window.alert = () => {};

  // jsdom 不实现媒体播放，打桩
  let playCalls = 0;
  const proto = window.HTMLMediaElement.prototype;
  Object.defineProperty(proto, 'paused', { get() { return this._paused !== false; }, configurable: true });
  Object.defineProperty(proto, 'seeking', { get() { return false; }, configurable: true });
  Object.defineProperty(proto, 'duration', { get() { return this._duration ?? 87; }, configurable: true });
  Object.defineProperty(proto, 'readyState', { get() { return 1; }, configurable: true });
  Object.defineProperty(proto, 'currentTime', {
    get() { return this._currentTime ?? 0; },
    set(value) { this._currentTime = value; },
    configurable: true,
  });
  proto.play = function play() {
    playCalls += 1;
    this._paused = false;
    this.dispatchEvent(new window.Event('play'));
    return Promise.resolve();
  };
  proto.pause = function pause() {
    this._paused = true;
    this.dispatchEvent(new window.Event('pause'));
  };

  // 模拟触摸滑动（jsdom 没有 TouchEvent，手工挂属性）
  const fireTouch = (el, type, y) => {
    const ev = new window.Event(type, { bubbles: true });
    Object.defineProperty(ev, 'touches', { value: type === 'touchend' ? [] : [{ clientY: y }] });
    Object.defineProperty(ev, 'changedTouches', { value: [{ clientY: y }] });
    el.dispatchEvent(ev);
  };

  console.log(`学生端模拟测试 ${BASE}\n`);

  console.log('1. 配对');
  window.eval(appJs);
  const pairShown = await waitFor(() => !doc.getElementById('screen-pair').classList.contains('hidden'));
  check('首次进入显示配对页', Boolean(pairShown));

  doc.getElementById('pair-code').value = '000000';
  doc.getElementById('pair-btn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const badCodeError = await waitFor(() => textOf(doc.getElementById('pair-error')));
  check('错误配对码被拒绝', Boolean(badCodeError), badCodeError || '');

  doc.getElementById('pair-code').value = pairCode;
  doc.getElementById('pair-btn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const homeShown = await waitFor(() => !doc.getElementById('screen-home').classList.contains('hidden'));
  check('正确配对码进入首页', Boolean(homeShown));
  check('显示学生姓名', textOf(doc.getElementById('hello-name')).includes(students[0].name),
    textOf(doc.getElementById('hello-name')));
  check('令牌已持久化', Boolean(window.localStorage.getItem('student_token')));

  console.log('\n2. 今日任务');
  const tasks = await waitFor(() => {
    const nodes = doc.querySelectorAll('#task-list .task[data-index]');
    return nodes.length ? nodes : null;
  });
  check('任务列表渲染', Boolean(tasks), `${tasks ? tasks.length : 0} 集`);
  check('显示目标分钟数', Number(textOf(doc.getElementById('target-min'))) > 0,
    `${textOf(doc.getElementById('target-min'))} 分钟`);
  check('进度环有描边值', doc.getElementById('ring-fg').style.strokeDashoffset !== '');

  // 三栏布局：序号 · 名称 · 时长
  const firstTask = doc.querySelector('#task-list .task[data-index]');
  const columns = Array.from(firstTask.children).map((el) => el.className);
  check('任务行分三栏（序号/名称/时长）',
    columns.length === 3 && columns[0] === 'idx' && columns[1] === 'name' && columns[2] === 'dur',
    columns.join(' | '));
  check('时长显示在名称右侧', /^\d+:\d{2}$/.test(textOf(firstTask.querySelector('.dur'))),
    textOf(firstTask.querySelector('.dur')));
  check('名称保留完整文本（靠 CSS 两行截断，不硬截）',
    firstTask.querySelector('.name').textContent.trim().length > 10,
    `${firstTask.querySelector('.name').textContent.trim().length} 字`);

  console.log('\n3. 抖音式播放器（默认看模式）');
  playCalls = 0;
  doc.getElementById('start-btn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('play() 在用户手势内同步调用', playCalls > 0, `同步调用 ${playCalls} 次`);
  const playerShown = await waitFor(() => !doc.getElementById('screen-player').classList.contains('hidden'));
  check('进入全屏播放器', Boolean(playerShown));

  const audio = doc.getElementById('audio');
  const video = doc.getElementById('video');
  const feed = doc.getElementById('screen-player');
  const stage = doc.getElementById('stage');

  check('默认是看模式（视频优先）',
    doc.querySelector('#mode-switch .mode-btn[data-mode="video"]').classList.contains('active'));
  check('播放视频地址（非音轨）',
    video.src.includes('/api/media/') && !video.src.includes('track=audio'),
    video.src.split('/api/')[1]?.slice(0, 34));
  check('视频元素可见', !video.classList.contains('hidden'));
  check('封面图隐藏', doc.getElementById('cover-art').classList.contains('hidden'));
  check('看模式显示遮字幕按钮', !doc.getElementById('btn-mask').classList.contains('hidden'));

  console.log('\n4. 顶部 HUD');
  check('显示集名', textOf(doc.getElementById('hud-name')).length > 0,
    textOf(doc.getElementById('hud-name')).slice(0, 24));
  check('显示进度 n/N', /^\d+\s*\/\s*\d+/.test(textOf(doc.getElementById('hud-progress'))),
    textOf(doc.getElementById('hud-progress')));
  check('右上角有常用按钮（自动音量/遮字幕/循环/更多）',
    ['btn-autolevel', 'btn-mask', 'btn-loop', 'btn-more'].every((id) => Boolean(doc.getElementById(id))));

  console.log('\n5. 展开面板');
  const moreBtn = doc.getElementById('btn-more');
  check('初始未展开', !feed.classList.contains('expanded'));
  moreBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('点「更多」展开', feed.classList.contains('expanded'));
  check('面板里有模式切换', Boolean(doc.getElementById('mode-switch')));
  check('面板里有音量滑块', Boolean(doc.getElementById('volume')));
  check('面板里有上一集/下一集', Boolean(doc.getElementById('btn-prev')) && Boolean(doc.getElementById('btn-next')));
  check('面板里有倍速与人声增强', Boolean(doc.getElementById('btn-rate')) && Boolean(doc.getElementById('btn-voice')));
  moreBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('再点收起', !feed.classList.contains('expanded'));

  console.log('\n6. 上下滑切换');
  const titleBefore = textOf(doc.getElementById('hud-name'));
  fireTouch(stage, 'touchstart', 600);
  fireTouch(stage, 'touchend', 300);        // 上滑 300px
  await sleep(420);
  const titleAfter = textOf(doc.getElementById('hud-name'));
  check('上滑切换到下一集', titleAfter !== titleBefore,
    `${titleBefore.slice(0, 16)} → ${titleAfter.slice(0, 16)}`);

  const beforeBack = textOf(doc.getElementById('hud-name'));
  fireTouch(stage, 'touchstart', 300);
  fireTouch(stage, 'touchend', 600);        // 下滑
  await sleep(420);
  check('下滑切回上一集', textOf(doc.getElementById('hud-name')) === beforeBack ? false : true,
    textOf(doc.getElementById('hud-name')).slice(0, 16));

  console.log('\n7. 切到听模式');
  const modeAudio = doc.querySelector('#mode-switch .mode-btn[data-mode="audio"]');
  modeAudio.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const audioMode = await waitFor(() => video.classList.contains('hidden'));
  check('切到听模式', Boolean(audioMode));
  check('听模式播音轨（track=audio）', audio.src.includes('track=audio'),
    audio.src.split('?')[1]?.slice(0, 30));
  check('听模式隐藏遮字幕按钮', doc.getElementById('btn-mask').classList.contains('hidden'));
  check('听模式遮罩关闭', doc.getElementById('subtitle-mask').classList.contains('off'));

  console.log('\n8. 切回看模式 + 遮字幕');
  doc.querySelector('#mode-switch .mode-btn[data-mode="video"]')
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => !video.classList.contains('hidden'));
  check('切回看模式', !video.classList.contains('hidden'));
  const maskBtn = doc.getElementById('btn-mask');
  maskBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('可开启遮字幕', !doc.getElementById('subtitle-mask').classList.contains('off'));
  check('遮罩默认高度 20%', doc.getElementById('subtitle-mask').style.height === '20%',
    doc.getElementById('subtitle-mask').style.height);
  check('遮罩高度滑块出现', !doc.getElementById('mask-slider-wrap').classList.contains('hidden'));
  maskBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('可关闭遮字幕', doc.getElementById('subtitle-mask').classList.contains('off'));

  console.log('\n9. 播放控制');
  check('播放中不显示中央播放键', !feed.classList.contains('paused'));
  video.pause();
  check('暂停时显示中央播放键', feed.classList.contains('paused'));
  video.play();
  doc.getElementById('btn-rate').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('倍速可切换', video.playbackRate !== 1.0, `${video.playbackRate}×`);
  doc.getElementById('btn-loop').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('单集循环可切换', video.loop === true && audio.loop === true);

  console.log('\n10. 音量、自动音量与人声增强');
  const volInput = doc.getElementById('volume');
  check('有音量滑块', Boolean(volInput), `默认 ${volInput ? volInput.value : '?'}%`);
  volInput.value = '60';
  volInput.dispatchEvent(new window.Event('input', { bubbles: true }));
  check('音量作用于当前播放元素', video.volume > 0 && video.volume <= 0.61,
    `video.volume=${video.volume.toFixed(3)}`);
  check('音量记忆到本地', window.localStorage.getItem('volume') === '60');

  const autoBtn = doc.getElementById('btn-autolevel');
  check('自动音量默认开启', autoBtn.classList.contains('on'));
  autoBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('可关闭自动音量',
    !autoBtn.classList.contains('on') && window.localStorage.getItem('auto_level') === '0');
  check('关闭后音量回到滑块值', Math.abs(video.volume - 0.6) < 0.01, `video.volume=${video.volume}`);
  autoBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('可重新开启自动音量', autoBtn.classList.contains('on'));

  const voiceBtn = doc.getElementById('btn-voice');
  doc.getElementById('toast').classList.add('hidden');
  voiceBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const voiceToast = await waitFor(() => {
    const el = doc.getElementById('toast');
    return el && !el.classList.contains('hidden') ? textOf(el) : null;
  });
  check('无 Web Audio 时给出提示', voiceToast === '这台设备不支持人声增强', voiceToast || '');

  console.log('\n11. 有效时长上报');
  video._currentTime = 10;
  video.dispatchEvent(new window.Event('timeupdate'));
  await sleep(3200);
  video._currentTime = 13;
  video.dispatchEvent(new window.Event('timeupdate'));

  doc.getElementById('player-back').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => !doc.getElementById('screen-home').classList.contains('hidden'));
  await sleep(600);

  check('返回列表后停止播放（音频）', audio.paused === true, `audio.paused=${audio.paused}`);
  check('返回列表后停止播放（视频）', video.paused === true, `video.paused=${video.paused}`);
  check('返回列表后收起展开面板', !feed.classList.contains('expanded'));

  const today = await apiAs(memberToken, `/api/report/${studentId}/today`);
  check('有效时长已计入今日报表', today.effective_min > 0, `${today.effective_min} 分钟`);
  const queueLeft = window.localStorage.getItem('event_queue');
  check('上报后队列已清空', !queueLeft || JSON.parse(queueLeft).length === 0, queueLeft || '空');

  console.log('\n12. 记录页');
  doc.querySelector('.tab[data-view="history"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const historyRows = await waitFor(() => {
    const nodes = doc.querySelectorAll('#history-view .row');
    return nodes.length >= 7 ? nodes : null;
  });
  check('记录页渲染 7 天', Boolean(historyRows), `${historyRows ? historyRows.length : 0} 行`);

  console.log('\n13. 过期令牌自愈');
  const dom2 = new JSDOM(html, { url: BASE + '/student.html', runScripts: 'outside-only', pretendToBeVisual: true });
  const win2 = dom2.window;
  win2.fetch = (input, init) => fetch(new URL(input, BASE), init);
  win2.confirm = () => true;
  win2.localStorage.setItem('student_token', 'stale-token-that-no-longer-exists');
  win2.eval(appJs);
  const backToPair = await waitFor(() => !win2.document.getElementById('screen-pair').classList.contains('hidden'));
  check('陈旧令牌自动回到配对页', Boolean(backToPair));
  check('给出「配对已失效」提示',
    textOf(win2.document.getElementById('pair-error')).includes('失效'),
    textOf(win2.document.getElementById('pair-error')));
  check('陈旧令牌已从本地清除', !win2.localStorage.getItem('student_token'));
  dom2.window.close();

  console.log(`\n${'='.repeat(46)}\n通过 ${pass}，失败 ${fail}`);
  dom.window.close();
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error('测试异常:', err);
  process.exit(1);
});
