// 管理课前教学设置、画像生成与覆盖确认，并在创建课堂前校验完整设定。
'use strict';
(() => {
  const el = id => document.getElementById(id);
  const requiredMessage = '请生成或填写学生设定，再创建试讲嗷~';
  let busy = false, classroomLocked = false;
  const groups = {
    grade: {'小学': ['一年级','二年级','三年级','四年级','五年级','六年级'], '初中': ['七年级','八年级','九年级'],
      '高中': ['高一','高二','高三'], '大学': ['大一','大二','大三','大四']},
    subject: {'学科': ['语文','数学','英语','物理','化学','生物','历史','地理','道德与法治','科学','信息科技','美术','音乐','体育']},
  };
  for (const [id, options] of Object.entries(groups)) {
    for (const [label, values] of Object.entries(options)) {
      const group = document.createElement('optgroup'); group.label = label;
      for (const value of values) group.append(new Option(value, value));
      el(id).append(group);
    }
    el(id).append(new Option('其他（自定义）', '__custom__'));
    el(id).addEventListener('change', () => showCustom(id));
    el(id + 'Custom').addEventListener('input', () => el(id + 'Custom').setCustomValidity(''));
  }

  function showCustom(id) {
    const custom = el(id).value === '__custom__';
    el(id + 'Custom').classList.toggle('hidden', !custom);
    el(id + 'CustomLabel').classList.toggle('hidden', !custom);
    el(id + 'Custom').setCustomValidity('');
  }
  function setChoice(id, value) {
    const known = [...el(id).options].some(option => option.value === value && value !== '__custom__');
    el(id).value = known ? value : '__custom__';
    if (!known) el(id + 'Custom').value = value;
    showCustom(id);
  }
  function status(message, error = false) {
    el('profileStatus').textContent = message;
    el('profileStatus').classList.toggle('failed', error);
  }
  function sync(locked = classroomLocked) {
    classroomLocked = locked;
    for (const input of el('lessonForm').querySelectorAll('input,textarea,select')) input.disabled = busy || locked;
    el('generateProfile').disabled = busy || locked || !el('topic').value.trim();
    el('generateProfile').textContent = busy ? '正在处理画像……' : '生成学生画像 ✧';
    el('studentProfile').setAttribute('aria-busy', String(busy));
  }
  function refresh() {
    sync();
    if (typeof syncControls === 'function') syncControls();
  }
  function settings() {
    if (!el('topic').value.trim()) {
      status('请先填写教学主题。', true); el('topic').focus(); return null;
    }
    const choices = {};
    for (const id of ['grade', 'subject']) {
      const custom = el(id).value === '__custom__';
      choices[id] = custom ? el(id + 'Custom').value.trim() : el(id).value;
      if (custom && !choices[id]) {
        el(id + 'Custom').setCustomValidity('请填写自定义内容，或选择「由 AI 判断」。');
        el(id + 'Custom').reportValidity(); return null;
      }
    }
    return {topic: el('topic').value.trim(), points: el('points').value, ...choices,
      student_level: document.querySelector('[name=studentLevel]:checked').value,
      lesson_type: document.querySelector('[name=lessonType]:checked').value, textbook: el('textbook').value};
  }
  function readLesson() {
    if (busy) return null;
    if (!el('studentProfile').value.trim()) {
      status(requiredMessage, true); el('studentProfile').focus(); return null;
    }
    const current = settings();
    return current ? {...current, student_profile: el('studentProfile').value} : null;
  }
  async function confirmOverwrite() {
    const dialog = el('profileOverwrite');
    dialog.returnValue = 'cancel';
    return new Promise(resolve => {
      dialog.addEventListener('close', () => resolve(dialog.returnValue === 'generate'), {once: true});
      dialog.showModal();
    });
  }
  async function generate() {
    if (busy || classroomLocked) return;
    const input = settings(); if (!input) return;
    // 从确认弹窗到回填结束保持同一把界面锁，避免双击与编辑竞态。
    busy = true; refresh();
    let timer;
    try {
      if (el('studentProfile').value.trim() && !await confirmOverwrite()) return;
      status('正在生成学生画像，请稍候……');
      const controller = new AbortController();
      timer = setTimeout(() => controller.abort(), 95000);
      const response = await fetch('/api/student-profile', {method: 'POST',
        headers: {'Content-Type': 'application/json'}, body: JSON.stringify(input), signal: controller.signal});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '教学设置不符合要求，请检查输入。');
      // 在修改任一表单字段前检查整个响应；未知或旧服务的响应不会清空草稿。
      for (const [key, max] of Object.entries({grade:100, subject:100, points:3000, student_profile:3000})) {
        if (typeof data[key] !== 'string' || !data[key].trim() || [...data[key]].length > max) throw new Error('生成结果格式无效，请重试。');
      }
      for (const id of ['grade', 'subject']) if (!input[id]) setChoice(id, data[id]);
      if (!input.points.trim()) el('points').value = data.points;
      el('studentProfile').value = data.student_profile;
      status('画像已生成。请检查并修改，点击「创建试讲」确认使用。');
    } catch (error) {
      status(error.name === 'AbortError' ? '生成超时，原有内容已保留，请重试。' : `生成失败，原有内容已保留。${error.message}`, true);
    } finally {
      clearTimeout(timer); busy = false; refresh();
    }
  }
  el('topic').addEventListener('input', refresh);
  el('studentProfile').addEventListener('input', () => status('点击「创建试讲」即确认使用当前画像。'));
  el('generateProfile').addEventListener('click', generate);
  window.studentProfile = {get busy() {return busy;}, sync, readLesson};
  sync();
})();
