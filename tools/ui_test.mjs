// 用 jsdom 真跑 app.js，模拟家长在网页上的操作，断言 DOM 渲染结果。
// 用法：node tools/ui_test.mjs [--base http://127.0.0.1:8010] [--password dev]

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
    } catch (err) {
      // 还没准备好，继续等
    }
    await sleep(interval);
  }
  return null;
}

function textOf(el) {
  return (el?.textContent || '').replace(/\s+/g, ' ').trim();
}

async function main() {
  const html = readFileSync(join(PROJECT, 'server/app/web/index.html'), 'utf8');
  const appJs = readFileSync(join(PROJECT, 'server/app/web/app.js'), 'utf8');

  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'outside-only',
    pretendToBeVisual: true,
  });
  const { window } = dom;

  // 相对路径请求要补上 base；直接复用 Node 的 fetch
  window.fetch = (input, init) => fetch(new URL(input, BASE), init);
  window.confirm = () => true;
  window.alert = () => {};

  console.log(`模拟浏览器访问 ${BASE}\n`);

  console.log('1. 页面加载与登录');
  window.eval(appJs);
  const loginVisible = await waitFor(() => !window.document.getElementById('login').classList.contains('hidden'));
  check('未登录时显示登录页', Boolean(loginVisible));
  check('主界面默认隐藏', window.document.getElementById('app').classList.contains('hidden'));

  const doc = window.document;

  // 先确认子账号存在（学生/计划只有子账号能管），再以子账号身份登录界面
  const memberUser = 'ui-member';
  const memberPwd = 'ui-member-123';
  const ownerLogin = await fetch(BASE + '/api/admin/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: 'parent', password: PASSWORD }),
  });
  if (ownerLogin.ok) {
    const { token: ownerToken } = await ownerLogin.json();
    const probe = await fetch(BASE + '/api/admin/login', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ username: memberUser, password: memberPwd }),
    });
    if (!probe.ok) {
      await fetch(BASE + '/api/accounts', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${ownerToken}` },
        body: JSON.stringify({ username: memberUser, password: memberPwd }),
      });
    }
  }

  doc.getElementById('username').value = memberUser;
  const passwordInput = doc.getElementById('password');
  passwordInput.value = memberPwd;
  doc.getElementById('login-form').dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));

  const appShown = await waitFor(() => !doc.getElementById('app').classList.contains('hidden'));
  check('登录成功后进入主界面', Boolean(appShown));

  console.log('\n2. 概览页');
  const card = await waitFor(() => doc.querySelector('#tab-dashboard .card'));
  check('渲染学生卡片', Boolean(card));
  const cardText = textOf(card);
  check('卡片含今日分钟数与目标', /\d+(\.\d+)?\s*\/\s*\d+\s*分钟/.test(cardText), cardText.slice(0, 60));
  check('卡片含连续天数', /连续\s*\d+\s*天/.test(cardText));
  const barCounts = [...doc.querySelectorAll('#tab-dashboard .card')]
    .map((el) => el.querySelectorAll('.week .day').length);
  check('每张卡片都有 7 天柱状图', barCounts.length > 0 && barCounts.every((n) => n === 7),
    `${barCounts.length} 个学生，每卡 ${barCounts.join('/')} 根`);

  console.log('\n3. 内容库页');
  const libraryTab = doc.querySelector('#tabs button[data-tab="library"]');
  libraryTab.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const items = await waitFor(() => {
    const nodes = doc.querySelectorAll('#tab-library .item');
    return nodes.length ? nodes : null;
  });
  check('视频列表渲染', Boolean(items), `${items ? items.length : 0} 集`);
  check('系列筛选按钮存在', doc.querySelectorAll('#tab-library [data-series]').length >= 2);
  check('难度筛选按钮存在', doc.querySelectorAll('#tab-library [data-level]').length === 6);
  check('列表项显示难度徽章', doc.querySelectorAll('#tab-library .item .badge').length > 0);
  check('列表项显示时长与语音占比', /语音/.test(textOf(doc.querySelector('#tab-library .item'))));

  // 点难度筛选 L3
  const l3Chip = doc.querySelector('#tab-library [data-level="L3"]');
  l3Chip.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const l3Items = await waitFor(() => {
    const nodes = doc.querySelectorAll('#tab-library .item');
    return nodes.length && [...nodes].every((n) => textOf(n).includes('L3')) ? nodes : null;
  });
  check('按 L3 筛选生效', Boolean(l3Items), `${l3Items ? l3Items.length : 0} 集全是 L3`);

  // 点回全部难度
  doc.querySelector('#tab-library [data-level=""]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => doc.querySelectorAll('#tab-library .item').length > 5);

  console.log('\n4. 视频详情弹层');
  const firstItem = doc.querySelector('#tab-library .item');
  firstItem.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const modalShown = await waitFor(() => !doc.getElementById('modal').classList.contains('hidden'));
  check('点击视频打开弹层', Boolean(modalShown));
  const transcriptText = await waitFor(() => {
    const el = doc.querySelector('#modal-body .transcript');
    return el && textOf(el).length > 20 ? el : null;
  });
  check('弹层显示转写全文', Boolean(transcriptText), transcriptText ? `${textOf(transcriptText).length} 字符` : '');
  const modalMeta = textOf(doc.getElementById('modal-body'));
  check('弹层显示难度与语速', /难度/.test(modalMeta) && /WPM/.test(modalMeta));
  doc.getElementById('modal-close').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('弹层可关闭', doc.getElementById('modal').classList.contains('hidden'));

  console.log('\n5. 计划页（一周排片视图）');
  doc.querySelector('#tabs button[data-tab="plan"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const weekCards = await waitFor(() => {
    const nodes = doc.querySelectorAll('#week-view .card');
    return nodes.length >= 7 ? nodes : null;
  });
  check('显示未来 7 天排片', Boolean(weekCards), `${weekCards ? weekCards.length : 0} 天`);
  check('有学生选择器', Boolean(doc.getElementById('plan-student')));
  check('有「重排今日」按钮', Boolean(doc.getElementById('plan-auto')));
  check('显示周一~周日目标汇总', /周一→周日/.test(textOf(doc.getElementById('plan-hint'))),
    textOf(doc.getElementById('plan-hint')));
  const firstDayText = textOf(weekCards ? weekCards[0] : null);
  check('每天显示目标与实际分钟', /目标\s*\d+\s*分钟/.test(firstDayText) && /实际/.test(firstDayText),
    firstDayText.slice(0, 40));
  check('今天有标记', firstDayText.includes('今天') || [...weekCards].some((c) => textOf(c).includes('今天')));

  // 重排今日
  doc.getElementById('toast').classList.add('hidden');
  doc.getElementById('plan-auto').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const autoToast = await waitFor(() => {
    const el = doc.getElementById('toast');
    const text = el && !el.classList.contains('hidden') ? textOf(el) : '';
    return text && (text.includes('已重排') || text.includes('没有可排')) ? text : null;
  });
  check('重排今日有反馈', Boolean(autoToast), autoToast || '');

  console.log('\n6. 设置页');
  doc.querySelector('#tabs button[data-tab="settings"]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const settingsReady = await waitFor(() => doc.getElementById('add-student'));
  check('设置页渲染', Boolean(settingsReady));
  check('学生区有配对码按钮', doc.querySelectorAll('#tab-settings [data-pair]').length > 0);
  check('设备区可撤销', doc.querySelectorAll('#tab-settings [data-revoke]').length > 0);
  check('子账号看不到内容维护', !doc.getElementById('do-scan'));
  check('子账号看不到「账号」标签',
    doc.querySelector('#tabs button[data-tab="accounts"]').classList.contains('hidden'));
  check('子账号看不到「内容库」标签',
    doc.querySelector('#tabs button[data-tab="library"]').classList.contains('hidden'));
  check('子账号能看到「计划」标签',
    !doc.querySelector('#tabs button[data-tab="plan"]').classList.contains('hidden'));

  // 学生管理：改每日目标时长
  // 学生管理：按周一到周日设置目标
  const editBtn = doc.querySelector('#tab-settings [data-edit-btn]');
  check('学生行有编辑按钮', Boolean(editBtn));
  const metaBefore = textOf(doc.querySelector('#tab-settings [data-student-row] [data-view] .meta'));
  check('列表显示周一~周日目标', /周一~周日/.test(metaBefore), metaBefore);
  editBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const editBox = doc.querySelector('#tab-settings [data-student-row] [data-edit]');
  check('点编辑出现输入框', !editBox.classList.contains('hidden'));
  const firstRow = doc.querySelector('#tab-settings [data-student-row]');
  const weekdayInputs = firstRow.querySelectorAll('[data-weekday]');
  check('有 7 个星期输入框', weekdayInputs.length === 7, `${weekdayInputs.length} 个`);

  const setAll = (value) => [...doc.querySelectorAll('#tab-settings [data-student-row]')[0]
    .querySelectorAll('[data-weekday]')].forEach((input) => { input.value = String(value); });
  const originalTarget = doc.querySelector('#tab-settings [data-student-row] [data-weekday]').value;
  setAll(35);
  doc.querySelector('#tab-settings [data-edit-save]')
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const metaAfter = await waitFor(() => {
    const el = doc.querySelector('#tab-settings [data-student-row] [data-view] .meta');
    const text = el ? textOf(el) : '';
    return text.includes('35') ? text : null;
  });
  check('保存后列表显示新目标', Boolean(metaAfter), `${metaBefore} → ${metaAfter || '未更新'}`);

  // 还原
  doc.querySelector('#tab-settings [data-edit-btn]')
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  setAll(originalTarget);
  doc.querySelector('#tab-settings [data-edit-save]')
    .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await waitFor(() => {
    const el = doc.querySelector('#tab-settings [data-student-row] [data-view] .meta');
    return el && textOf(el).includes(originalTarget);
  });


  // 生成配对码
  doc.querySelector('#tab-settings [data-pair]').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const pairCode = await waitFor(() => {
    const body = textOf(doc.getElementById('modal-body'));
    const match = body.match(/\b(\d{6})\b/);
    return match ? match[1] : null;
  });
  check('点击配对码按钮生成 6 位码', Boolean(pairCode), pairCode || '');
  doc.getElementById('modal-close').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));

  console.log('\n7. 退出登录');
  doc.getElementById('logout').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  check('退出后回到登录页', !doc.getElementById('login').classList.contains('hidden'));
  check('令牌已清除', !window.localStorage.getItem('token'));

  if (args.includes('--dump')) {
    console.log('\n' + '='.repeat(46) + '\n各页面渲染文本（模拟截图）\n');
    passwordInput.value = PASSWORD;
    doc.getElementById('login-form').dispatchEvent(new window.Event('submit', { cancelable: true, bubbles: true }));
    await waitFor(() => !doc.getElementById('app').classList.contains('hidden'));

    for (const [tab, selector] of [['dashboard', '#tab-dashboard'], ['library', '#tab-library'],
      ['plan', '#tab-plan'], ['settings', '#tab-settings']]) {
      doc.querySelector(`#tabs button[data-tab="${tab}"]`)
        .dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
      await waitFor(() => doc.querySelector(`${selector} .card`), { timeout: 6000 });
      await sleep(300);
      console.log(`\n--- ${tab} ---`);
      console.log(textOf(doc.querySelector(selector)).slice(0, 700));
    }
  }

  console.log(`\n${'='.repeat(46)}\n通过 ${pass}，失败 ${fail}`);
  dom.window.close();
  process.exit(fail === 0 ? 0 : 1);
}

main().catch((err) => {
  console.error('测试异常:', err);
  process.exit(1);
});