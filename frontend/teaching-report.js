'use strict';
(() => {
  const element = id => document.getElementById(id);
  let session = '', generation = 0, request = 0, timer, busy = false, result = null, autoWait = 0;

  function node(tag, text) {
    const item = document.createElement(tag);
    item.textContent = text;
    return item;
  }

  function controls() {
    element('generateTeachingReport').disabled = busy || ['ready', 'generating'].includes(result?.status);
    element('generateTeachingReport').hidden = autoWait > 0 || ['ready', 'generating'].includes(result?.status);
    element('generateTeachingReport').textContent = result?.status === 'failed' ? '重试教学报告' : '生成教学报告';
    element('reloadTeachingReport').disabled = busy;
    element('exportTeachingReport').disabled = result?.status !== 'ready';
  }

  function render(data) {
    result = data;
    element('teachingReportStatus').textContent = {
      idle: '试讲已结束，可以生成教学报告。', generating: '正在生成教学报告，请稍等……',
      ready: '教学报告已生成，可以查看和导出。', failed: data.error || '教学报告未生成成功，可以重试。'
    }[data.status];
    if (data.status === 'generating') element('teachingReportStatus').textContent = data.progress || '正在生成教学报告，请稍等……';
    if (data.status === 'idle' && autoWait > 0) element('teachingReportStatus').textContent = '正在结束课堂并准备报告……';
    const body = element('teachingReportBody');
    body.replaceChildren();
    if (data.status === 'ready' && data.report) {
      const report = data.report;
      body.append(node('h3', '总体概括'), node('p', report.overview.text));
      const dimensions = document.createElement('ul');
      dimensions.className = 'report-dimensions';
      for (const [key, name] of [['accuracy', '内容准确'], ['clarity', '讲解清楚'],
        ['coherence', '结构连贯'], ['checking', '师生互动']]) {
        const dim = report.dimensions[key], row = node('li', '');
        row.append(node('strong', name + ' · ' + dim.label), node('span', dim.reason));
        dimensions.append(row);
      }
      const finding = report.finding, practice = report.practice;
      body.append(dimensions, node('h3', '最值得注意的发现'),
        node('p', finding.kind + '：' + finding.text), node('p', '影响：' + finding.impact),
        node('p', '值得保留：' + (report.keep?.text || '材料不足，暂不单列优点。')),
        node('h3', '应该如何改'), node('p', report.action.text), node('h3', '下次怎么办'),
        node('p', `${practice.minutes}分钟练习：${practice.task}`), node('p', '完成标准：' + practice.criterion));
    }
    controls();
  }

  async function load(method = 'GET') {
    const sid = session, token = generation, sequence = ++request;
    clearTimeout(timer);
    busy = true; controls();
    try {
      const response = await fetch(`/api/teaching-report/${sid}`, {method, cache: 'no-store'});
      const data = await response.json();
      if (token !== generation || sequence !== request) return;
      if (!response.ok) throw new Error(data.detail || '读取教学报告失败');
      if (data.session_id !== sid) throw new Error('报告课堂编号不一致');
      if (data.status !== 'idle') autoWait = 0;
      render(data);
      if (data.status === 'generating' || (data.status === 'idle' && autoWait-- > 0)) timer = setTimeout(() => load(), 1000);
    } catch (error) {
      if (token !== generation || sequence !== request) return;
      element('teachingReportStatus').textContent = error.message + '；可点击“重新查看”。';
    } finally {
      if (token === generation && sequence === request) { busy = false; controls(); }
    }
  }

  function reset() {
    generation++; clearTimeout(timer); session = ''; result = null; busy = false; autoWait = 0;
    element('teachingReport').classList.add('hidden');
    element('teachingReportBody').replaceChildren();
    const url = new URL(location.href);
    url.searchParams.delete('teaching_report');
    history.replaceState(null, '', url);
    controls();
  }

  function open(sid, afterFinish = false) {
    reset(); session = sid; autoWait = afterFinish ? 30 : 0;
    element('teachingReport').classList.remove('hidden');
    const url = new URL(location.href);
    url.searchParams.set('teaching_report', sid);
    history.replaceState(null, '', url);
    load();
  }

  function unavailable(sid, message) {
    if (session !== sid) return;
    request++; busy = false; autoWait = 0; clearTimeout(timer);
    render({session_id: sid, status: 'failed', report: null, error: message});
  }

  element('generateTeachingReport').addEventListener('click', () => load('POST'));
  element('reloadTeachingReport').addEventListener('click', () => load());
  element('exportTeachingReport').addEventListener('click', () => {
    if (result?.status !== 'ready') return;
    const anchor = document.createElement('a');
    anchor.href = `/api/teaching-report/${session}/export`; anchor.download = `试讲教学报告-${session}.md`;
    anchor.click();
  });
  window.teachingReport = {open, reset, unavailable};
  const sid = new URLSearchParams(location.search).get('teaching_report');
  // Refresh may happen between the finished event and backend registration.
  // Waiting uses GET only, including this narrow resource-cleanup window.
  if (/^[0-9a-f]{12}$/.test(sid || '')) open(sid, true);
})();
