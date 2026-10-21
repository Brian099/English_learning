// 学生端网页的 jsdom 测试：配对 → 今日任务 → 听/看双模式 → 有效时长上报 → 记录
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
  const parentToken = (await apiAs(null, '/api/admin/login', 'POST', { password: PASSWORD })).token;
  const students = await apiAs(parentToken, '/api/students');
  if (!students.length) throw new Error('没有学生，请先跑 tools/seed_dev.py');
  const studentId = students[0].id;
  const pairCode = (await apiAs(parentToken, '/api/devices/pair-code', 'POST',
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
  check('显示已完成集数', /已完成 \d+ \/ \d+ 集/.test(textOf(doc.getElementById('done-count'))),
    textOf(doc.getElementById('done-count')));
  check('进度环有描边值', doc.getElementById('ring-fg').style.strokeDashoffset !== '');

  console.log('\n3. 播放器（听模式）');
  playCalls = 0;
  doc.getElementById('start-btn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  // 关键回归点：play() 必须在点击的同步调用栈里被调用。
  // 如果 openPlayer 中间插了 await，用户手势授权就丢了，
  // Chrome 会以 NotAllowedError 拒绝自动播放，表现为"点了没反应、也不发媒体请求"。
  check('play() 在用户手势内同步调用', playCalls > 0, `点击后同步调用 ${playCalls} 次`);
  const playerShown = await waitFor(() => !doc.getElementById('screen-player').classList.contains('hidden'));
  check('进入播放器', Boolean(playerShown));

  const audio = doc.getElementById('audio');
  const video = doc.getElementById('video');
  const mask = doc.getElementById('subtitle-mask');

  check('默认是听模式', doc.querySelector('#mode-switch .mode-btn[data-mode="audio"]').classList.contains('active'));
  check('听模式播放音轨（track=audio）', audio.src.includes('track=audio'), audio.src.split('?')[1]?.slice(0, 40));
  check('音频地址带设备令牌', audio.src.includes('token='));
  check('听模式隐藏视频元素', video.classList.contains('hidden'));
  check('听模式显示封面图', !doc.getElementById('cover-art').classList.contains('hidden'));
  check('听模式隐藏遮字幕按钮', doc.getElementById('btn-mask').classList.contains('hidden'));
  check('听模式遮罩不显示', mask.classList.contains('off'));
  check('提示熄屏可继续听', textOf(doc.getElementById('player-tip')).includes('熄屏也能继续听'),
    textOf(doc.getElementById('player-tip')));

  console.log('\n4. 切换到看模式');
  audio._currentTime = 5;
  audio.dispatchEvent(new window.Event('timeupdate'));
  doc.querySelector('#mode-switch .mode-btn[data-mode="video"]')
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const switched = await waitFor(() => !video.classList.contains('hidden'));
  check('切换到看模式', Boolean(switched));
  check('看模式播放视频（非音轨地址）',
    video.src.includes('/api/media/') && !video.src.includes('track=audio'),
    video.src.split('/api/')[1]?.slice(0, 40));
  check('看模式隐藏封面图', doc.getElementById('cover-art').classList.contains('hidden'));
  check('看模式显示遮字幕按钮', !doc.getElementById('btn-mask').classList.contains('hidden'));
  check('切换后保持播放进度', Math.abs((video._currentTime || 0) - 5) < 0.5,
    `audio 5s → video ${video._currentTime}s`);
  check('看模式提示熄屏会暂停', textOf(doc.getElementById('player-tip')).includes('熄屏会暂停'),
    textOf(doc.getElementById('player-tip')));

  console.log('\n5. 看模式下遮字幕');
  doc.getElementById('btn-mask').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('可开启遮字幕', !mask.classList.contains('off'));
  check('遮罩默认高度 20%', mask.style.height === '20%', mask.style.height);
  check('遮罩高度可调', !doc.getElementById('mask-slider-wrap').classList.contains('hidden'));
  doc.getElementById('btn-mask').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('可关闭遮字幕', mask.classList.contains('off'));

  console.log('\n6. 切回听模式');
  video._currentTime = 9;
  video.dispatchEvent(new window.Event('timeupdate'));
  doc.querySelector('#mode-switch .mode-btn[data-mode="audio"]')
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  const backToAudio = await waitFor(() => video.classList.contains('hidden'));
  check('切回听模式', Boolean(backToAudio));
  check('听模式重新使用音轨地址', audio.src.includes('track=audio'));
  check('切回后保持播放进度', Math.abs((audio._currentTime || 0) - 9) < 0.5,
    `video 9s → audio ${audio._currentTime}s`);
  check('听模式遮罩自动隐藏', mask.classList.contains('off'));

  console.log('\n7. 播放控制');
  check('播放中按钮显示暂停图标', textOf(doc.getElementById('btn-play')) === '❚❚',
    textOf(doc.getElementById('btn-play')));
  doc.getElementById('btn-rate').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('倍速可切换', audio.playbackRate !== 1.0, `${audio.playbackRate}×`);
  doc.getElementById('btn-loop').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('单集循环可切换', audio.loop === true && video.loop === true);

  console.log('\n8. 有效时长上报');
  audio._currentTime = 10;
  audio.dispatchEvent(new window.Event('timeupdate'));
  await sleep(3200);
  audio._currentTime = 13;
  audio.dispatchEvent(new window.Event('timeupdate'));

  doc.getElementById('player-back').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => !doc.getElementById('screen-home').classList.contains('hidden'));
  await sleep(600);

  const today = await apiAs(parentToken, `/api/report/${studentId}/today`);
  check('有效时长已计入今日报表', today.effective_min > 0, `${today.effective_min} 分钟`);
  const queueLeft = window.localStorage.getItem('event_queue');
  check('上报后队列已清空', !queueLeft || JSON.parse(queueLeft).length === 0, queueLeft || '空');

  console.log('\n9. 记录页');
  doc.querySelector('.tab[data-view="history"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const historyRows = await waitFor(() => {
    const nodes = doc.querySelectorAll('#history-view .row');
    return nodes.length >= 7 ? nodes : null;
  });
  check('记录页渲染 7 天', Boolean(historyRows), `${historyRows ? historyRows.length : 0} 行`);
  check('记录页显示合计', /近 7 天共/.test(textOf(doc.getElementById('history-view'))));

  console.log(`\n${'='.repeat(46)}\n通过 ${pass}，失败 ${fail}`);
  dom.window.close();
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error('测试异常:', err);
  process.exit(1);
});
