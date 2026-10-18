'use strict';
(() => {
  const el = id => document.getElementById(id);
  let generation = 0, timer, reportTimer, sid = '', before = null, offset = 0, revision = '', loading = false, reporting = false;
  const text = (tag, value, className) => {
    const node = document.createElement(tag); node.textContent = value;
    if (className) node.className = className;
    return node;
  };
  function leave() { generation++; clearTimeout(timer); clearTimeout(reportTimer); }
  async function get(url, token, method = 'GET') {
    const response = await fetch(url, {method, cache: 'no-store'});
    const data = await response.json();
    if (token !== generation) return null;
    if (!response.ok) throw new Error(data.detail || '读取失败，请刷新重试。');
    return data;
  }
  function date(value) {
    const parsed = new Date(value);
    return value && !Number.isNaN(parsed.valueOf()) ? parsed.toLocaleString('zh-CN', {hour12: false}) : '未记录';
  }
  function navigate(path) { history.pushState(null, '', path); open(); }
  async function list(token, more = false) {
    el('moreHistory').disabled = true;
    try {
      const data = await get(`/api/history?offset=${more ? offset : 0}`, token);
      if (!data) return;
      if (!more) { el('historyCards').replaceChildren(); offset = 0; }
      for (const item of data.items) {
        const button = document.createElement('button'); button.className = 'history-card';
        button.append(text('strong', item.topic), text('span', `${item.grade} · ${item.subject}`), text('time', date(item.started_at)));
        button.onclick = () => navigate(`/history/${item.session_id}`);
        el('historyCards').append(button);
      }
      offset += data.items.length;
      el('moreHistory').classList.toggle('hidden', offset >= data.total);
      el('historyNotice').textContent = data.indexing ? '正在整理已有课堂，列表完成后会自动更新……' : data.total ? '' : '还没有已保存的课堂记录。';
      if (data.indexing) timer = setTimeout(() => list(token), 1200);
    } catch (error) { if (token === generation) el('historyNotice').textContent = error.message; }
    finally { if (token === generation) el('moreHistory').disabled = false; }
  }
  async function dialogue(token, earlier = false) {
    if (loading) return;
    loading = true; el('earlierHistory').disabled = true;
    try {
      const data = await get(`/api/history/${sid}/dialogue${earlier ? '?before=' + before : ''}`, token);
      if (!data) return;
      if (data.status !== 'ready') { timer = setTimeout(() => detail(token), 1000); return; }
      if (earlier && revision !== data.revision) { loading = false; await detail(token); return; }
      revision = data.revision;
      const container = el('historyDialogue'), fragment = document.createDocumentFragment();
      for (const item of data.items) {
        const row = document.createElement('article'); row.className = `history-turn ${item.role}`;
        const label = item.role !== 'teacher' ? '学生' : item.request_count ? '老师 · 讲授批次' : '老师 · 发送批次未确认';
        row.append(text('small', `${label}${item.time ? ' · ' + date(item.time) : ''}`), text('p', item.text, 'history-text'));
        if (item.processing_status === 'failed') row.append(text('p', '学生处理失败，此批讲授内容已保留。', 'history-processing'));
        else if (item.processing_status === 'submitted') row.append(text('p', '已发起学生处理请求，未找到成功处理记录。', 'history-processing'));
        else if (item.had_failure) row.append(text('p', '此前处理失败，重试后已处理。', 'history-processing'));
        if (item.role === 'teacher' && (item.segments?.length || item.raw_text !== item.text)) {
          const details = document.createElement('details');
          const segments = item.segments || [{raw_text: item.raw_text, time: item.time, elapsed: item.elapsed}];
          details.append(text('summary', `查看原始转写（${segments.length} 段）`));
          // Large batches should not create all provenance nodes until expanded.
          details.addEventListener('toggle', () => {
            if (!details.open || details.childElementCount > 1) return;
            for (const segment of segments) {
              const original = document.createElement('div'); original.className = 'history-original';
              const stamp = segment.time ? date(segment.time) : typeof segment.elapsed === 'number' ? `${segment.elapsed.toFixed(1)}s` : '时间未记录';
              original.append(text('small', stamp), text('p', segment.raw_text ?? '此片段的原始转写未记录。', 'history-text'));
              details.append(original);
            }
          });
          row.append(details);
        }
        fragment.append(row);
      }
      const oldTop = window.scrollY, oldHeight = container.offsetHeight;
      if (earlier) container.prepend(fragment); else container.replaceChildren(fragment);
      if (!data.total) container.append(text('p', '没有已保存的师生对话。'));
      before = data.before; el('earlierHistory').classList.toggle('hidden', before === null);
      if (earlier) window.scrollTo({top: oldTop + container.offsetHeight - oldHeight, behavior: 'instant'});
    } catch (error) { if (token === generation) el('historyNotice').textContent = error.message; }
    finally { if (token === generation) { loading = false; el('earlierHistory').disabled = false; } }
  }
  async function report(token, method = 'GET', auto = true) {
    if (reporting) return;
    reporting = true; el('retryHistoryReport').disabled = true;
    try {
      let data = await get(`/api/teaching-report/${sid}`, token, method);
      if (!data) return;
      if (data.status === 'idle' && auto) data = await get(`/api/teaching-report/${sid}`, token, 'POST');
      if (!data) return;
      el('historyReportStatus').textContent = data.status === 'ready' ? '教学报告已生成。' : data.status === 'failed' ? data.error || '报告生成失败，可重试。' : data.progress || '正在生成教学报告……';
      window.teachingReport.renderBody(el('historyReportBody'), data);
      el('retryHistoryReport').classList.toggle('hidden', !['failed', 'idle'].includes(data.status));
      if (data.status === 'generating') reportTimer = setTimeout(() => report(token, 'GET', false), 1500);
    } catch (error) {
      if (token === generation) { el('historyReportStatus').textContent = error.message; el('retryHistoryReport').classList.remove('hidden'); }
    } finally { if (token === generation) { reporting = false; el('retryHistoryReport').disabled = false; } }
  }
  async function detail(token) {
    try {
      const data = await get(`/api/history/${sid}`, token);
      if (!data) return;
      if (data.status !== 'ready') { el('historyNotice').textContent = data.message; timer = setTimeout(() => detail(token), 1000); return; }
      el('historyNotice').textContent = '';
      el('historyTopic').textContent = data.topic;
      el('historyWarning').textContent = data.warnings.join(' ');
      el('historyProfile').textContent = data.student_profile || '这堂课未保存学生画像。';
      el('downloadHistory').href = `/api/history/${sid}/log`;
      el('downloadHistory').download = `classroom-${sid}.jsonl`;
      void dialogue(token);
      if (data.report_unavailable) el('historyReportStatus').textContent = data.report_unavailable;
      else void report(token);
    } catch (error) { if (token === generation) el('historyNotice').textContent = error.message; }
  }
  function open() {
    leave(); const token = generation;
    sid = /^\/history\/([0-9a-f]{12})$/.exec(location.pathname)?.[1] || '';
    loading = reporting = false; before = null; revision = ''; offset = 0;
    el('historyListView').classList.toggle('hidden', Boolean(sid));
    el('historyDetail').classList.toggle('hidden', !sid);
    el('historyNotice').textContent = '正在读取课堂记录……';
    el('historyTopic').textContent = ''; el('historyProfile').textContent = '';
    el('historyWarning').textContent = ''; el('historyDialogue').replaceChildren();
    el('historyReportBody').replaceChildren(); el('historyReportStatus').textContent = '';
    el('retryHistoryReport').classList.add('hidden'); el('earlierHistory').classList.add('hidden');
    el('downloadHistory').removeAttribute('href');
    if (sid) void detail(token); else void list(token);
  }
  el('backHistory').onclick = () => navigate('/history');
  el('refreshHistory').onclick = open;
  el('moreHistory').onclick = () => list(generation, true);
  el('earlierHistory').onclick = () => dialogue(generation, true);
  el('retryHistoryReport').onclick = () => report(generation, 'POST', false);
  window.classroomHistory = {open, leave};
  if (location.pathname.startsWith('/history')) open();
})();
