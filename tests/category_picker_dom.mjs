// 纯内存 DOM 单元测试，不启动浏览器、不登录网站。
import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import vm from 'node:vm';
import test from 'node:test';

const script = readFileSync(new URL('../app/static/js/category-picker.js', import.meta.url), 'utf8');

function fixture(mainValue = '1', subValue = '11') {
  const main = {value: mainValue, listeners: {}, addEventListener(type, handler) { this.listeners[type] = handler; }};
  const sub = {value: subValue, disabled: false, options: [],
    replaceChildren(...options) { this.options = options; },
    add(option) { this.options.push(option); }};
  const hint = {textContent: ''};
  const events = {};
  const data = [
    {id: 11, parent_id: 1, name: '网络与通俗小说'},
    {id: 12, parent_id: 1, name: '<img src=x onerror=alert(1)>'},
    {id: 21, parent_id: 2, name: '人工智能'},
  ];
  const elements = {'[data-category-main]': main, '[data-category-sub]': sub,
    '[data-category-hint]': hint, '[data-category-options]': {textContent: JSON.stringify(data)}};
  const picker = {querySelector(selector) { return elements[selector]; }};
  vm.runInNewContext(script, {
    document: {querySelectorAll() { return [picker]; }},
    window: {addEventListener(type, handler) { events[type] = handler; }},
    Option: class { constructor(text, value) { this.textContent = text; this.value = value; } },
  });
  return {main, sub, hint, events, change(value) { main.value = value; main.listeners.change(); }};
}

test('首次显示保留选中二级，并且只展示同一级下的选项', () => {
  const f = fixture();
  assert.equal(f.sub.value, '11');
  assert.deepEqual(f.sub.options.map(o => o.value), ['', '11', '12']);
  assert.equal(f.sub.disabled, false);
  assert.equal(f.sub.options[2].textContent, '<img src=x onerror=alert(1)>');
});

test('切换一级清空旧二级、只显示新一级的选项', () => {
  const f = fixture();
  f.change('2');
  assert.equal(f.sub.value, '');
  assert.deepEqual(f.sub.options.map(o => o.value), ['', '21']);
  assert.equal(f.sub.disabled, false);
});

test('未选择一级及无二级的大类均禁用二级控件', () => {
  const f = fixture();
  f.change('');
  assert.equal(f.sub.disabled, true);
  assert.deepEqual(f.sub.options.map(o => o.value), ['']);
  f.change('3');
  assert.equal(f.sub.disabled, true);
  assert.match(f.hint.textContent, /暂无二级分类/);
});

test('保留异常旧分类不自动选择新分类', () => {
  const f = fixture('__keep__', '');
  assert.equal(f.main.value, '__keep__');
  assert.equal(f.sub.disabled, true);
  assert.match(f.hint.textContent, /保留现有分类/);
  f.change('2');
  assert.equal(f.sub.disabled, false);
});

test('返回页面时清理浏览器恢复的不匹配二级', () => {
  const f = fixture();
  f.main.value = '2';
  f.events.pageshow();
  assert.equal(f.sub.value, '');
  assert.deepEqual(f.sub.options.map(o => o.value), ['', '21']);
});
