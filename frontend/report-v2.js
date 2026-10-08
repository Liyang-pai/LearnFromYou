// 课后报告 V2：即时结算、异步生成、证据定位、重试和 Markdown 导出。
(() => {
  'use strict';
  const el = id => document.getElementById(id);
  const state = {sid: '', token: 0, timer: null, view: null};
  const phaseNames = {recall:'学生知识复述', answers:'学生独立作答', verification:'理解验证', diagnosis:'教学反馈'};
  const statusNames = {idle:'基础结算已就绪，点击生成学生复述与反馈。', generating:'正在生成，基础结算可先查看。', ready:'报告已生成。', partial:'部分分析未完成，已成功的内容保留，可重试。', failed:'生成未完成，基础结算保留，可重试。'};
  function node(tag, text, cls) {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  }
  function reset() {
    state.token++; state.sid = ''; state.view = null; clearTimeout(state.timer);
    el('reportV2').classList.add('hidden');
    for (const id of ['reportSummary','reportAnalysis','reportEvidence','reportStages']) el(id).replaceChildren();
    el('reportDownload').disabled = true;
  }
  function settlement(summary) {
    const target = el('reportSummary'); target.replaceChildren();
    const grid = node('div', undefined, 'report-metrics');
    for (const item of Object.values(summary.metrics)) {
      const card = node('div', undefined, 'report-metric');
      card.append(node('span', item.label), node('strong', item.value === null ? '无法统计' : String(item.value)));
      const method = node('details'); method.append(node('summary','统计口径'), node('p',item.method,'hint'));
      card.append(method); grid.append(card);
    }
    target.append(grid, node('p', summary.incomplete ? '反馈可能不完整：存在未处理讲授或录音识别缺口，不能算作学生已经学到。请核对是否已重讲。' : '课堂已完成收尾。当前理解是课堂推断，不是独立验证。', summary.incomplete ? 'report-warning' : 'hint'));
    target.append(node('h3','这次实际涉及的知识'));
    const statuses = {understood:'当前理解', tentative:'暂定理解', conflict:'存在冲突', unclear:'信息缺失'};
    for (const point of summary.knowledge_points) target.append(node('p',`${point.text} · ${statuses[point.status] || point.status}`));
    if (!summary.knowledge_points.length) target.append(node('p','没有可靠的已处理知识记录。','hint'));
    target.append(node('h3','疑问与处理情况'));
    const qlabels = {pending:'待提问', asked:'已提问', resolved:'系统标记已解决', deferred:'暂缓'};
    for (const q of summary.questions) target.append(node('p',`${q.text} · ${qlabels[q.status] || q.status} · 教师解释来源：${q.resolution_sources?.join('、') || '未记录'}`));
    if (!summary.questions.length) target.append(node('p','未记录学生疑问。','hint'));
    if (summary.unprocessed_sources.length) target.append(node('p','未处理讲授：' + summary.unprocessed_sources.join('、'),'report-warning'));
    for (const error of summary.processing_errors) target.append(node('p','处理异常记录：' + error.text,'hint'));
    if (summary.processing_errors.length) target.append(node('p','异常记录可能已恢复，并不全部等于最终失败。','hint'));
  }
  function evidenceKey(id) { return 'report-evidence-' + id.replace(/[^a-zA-Z0-9_-]/g,'-'); }
  function locate(id, quote) {
    const row = document.getElementById(evidenceKey(id)); if (!row) return;
    document.querySelectorAll('.report-highlight').forEach(n => n.classList.remove('report-highlight'));
    row.open = true; row.classList.add('report-highlight');
    const body = row.querySelector('[data-evidence-text]'), text = state.view.snapshot.evidence[id].text;
    const index = text.indexOf(quote); body.replaceChildren();
    if (quote && index >= 0) body.append(document.createTextNode(text.slice(0,index)), node('mark',quote), document.createTextNode(text.slice(index + quote.length)));
    else body.textContent = text;
    row.scrollIntoView({behavior:'smooth',block:'center'}); row.focus();
  }
  function citations(parent, items) {
    const refs = node('div',undefined,'report-citations');
    for (const c of items || []) {
      const button = node('button','查看证据 · ' + c.event_id,'small'); button.type = 'button';
      button.onclick = () => locate(c.event_id,c.quote); refs.append(button);
    }
    parent.append(refs);
  }
  function section(title) {
    const n = node('section',undefined,'report-section'); n.append(node('h3',title)); el('reportAnalysis').append(n); return n;
  }
  function analysis(view) {
    el('reportAnalysis').replaceChildren();
    const recall = section('学生说，我学到了什么');
    recall.append(node('p','这是模拟学生的表达，不能仅凭复述就认为已经理解。','hint'));
    if (!view.recall) recall.append(node('p','尚未生成或生成失败，暂不展示复述。'));
    else for (const [key,title] of [['explained','我能解释的'],['doubts','我还有疑问的'],['uncertain','我不确定的']]) {
      recall.append(node('h4',title));
      for (const item of view.recall[key]) { const block = node('div',undefined,'report-item'); block.append(node('p',item.text)); citations(block,item.citations); recall.append(block); }
      if (!view.recall[key].length) recall.append(node('p','暂无有依据的内容。','hint'));
    }
    const verification = section('学生真的理解了吗');
    verification.append(node('p','模拟验证只提供课堂内解释或应用的证据，不能证明真实掌握或知识客观正确。','hint'));
    const answers = new Map((view.answers?.answers || []).map(a => [a.id,a]));
    const results = new Map((view.verification?.results || []).map(r => [r.id,r]));
    for (const probe of view.recall?.probes || []) {
      const block = node('div',undefined,'report-item'), answer = answers.get(probe.id), result = results.get(probe.id);
      block.append(node('h4',probe.question), node('p','学生回答：' + (answer?.text || '尚未验证')), node('strong',result?.status || '尚未验证'), node('p',result?.explanation || '验证尚未完成或失败。'));
      citations(block,[...probe.citations,...(answer?.citations || []),...(result?.citations || [])]); verification.append(block);
    }
    if (!view.recall?.probes.length) verification.append(node('p','尚无可靠验证题，不推断已经理解。'));
    for (const [key,title] of [['strengths','本次试讲的优点'],['weaknesses','本次试讲的不足']]) {
      const target = section(title);
      for (const finding of view.diagnosis?.[key] || []) {
        const block = node('div',undefined,'report-item'); block.append(node('p','观察事实：' + finding.observation),node('p','AI 推断：' + finding.interpretation,'hint'));
        citations(block,finding.citations); target.append(block);
      }
      if (!(view.diagnosis?.[key] || []).length) target.append(node('p','没有足够证据作出判断，或分析尚未完成。'));
    }
    const suggestions = section('下次应该怎么改');
    for (const suggestion of view.diagnosis?.suggestions || []) {
      const block = node('div',undefined,'report-item'), finding = view.diagnosis.weaknesses.find(f => f.id === suggestion.finding_id);
      block.append(node('strong',suggestion.priority + ' · 下次尝试'),node('p',suggestion.action),node('p','针对：' + finding.observation,'hint'));
      citations(block,finding.citations); suggestions.append(block);
    }
    if (!view.diagnosis?.suggestions.length) suggestions.append(node('p','暂无有依据的建议。'));
  }
  function evidence(snapshot) {
    const target = el('reportEvidence'); target.replaceChildren();
    const kinds = {transcript:'教师讲授', reply:'学生发言', review_completed:'转写审核', ready:'课堂开始', state:'学生状态', finished:'最终状态', error:'处理异常'};
    for (const item of Object.values(snapshot.evidence)) {
      const row = node('details',undefined,'report-evidence'); row.id = evidenceKey(item.event_id); row.tabIndex = -1;
      row.append(node('summary',`${kinds[item.kind] || item.kind} · ${item.at === null || item.at === undefined ? '记录 ' + item.order : item.at + ' 秒'} · ${item.event_id}`));
      const body = node('p',item.text); body.setAttribute('data-evidence-text',''); row.append(body);
      if (item.raw_text !== undefined) {
        row.append(node('p','原始转写：' + item.raw_text,'hint'),node('p','实际采用文本：' + item.text,'hint'));
        if (item.review_event_id) row.append(node('p','审核对应记录：' + item.review_event_id,'hint'));
      }
      target.append(row);
    }
  }
  function render(view) {
    state.view = view; settlement(view.snapshot.settlement); analysis(view); evidence(view.snapshot);
    el('reportStatus').textContent = statusNames[view.status] || '报告状态未知';
    el('reportGenerate').disabled = view.status === 'generating' || view.status === 'ready';
    el('reportGenerate').textContent = ['partial','failed'].includes(view.status) ? '重试未完成分析' : '生成学生复述与反馈';
    el('reportDownload').disabled = !view.markdown; el('reportStages').replaceChildren();
    const labels = {pending:'尚未生成', generating:'生成中', success:'已生成', skipped:'无可靠题目，尚未验证', failed:'未完成'};
    for (const [phase,stage] of Object.entries(view.stages)) el('reportStages').append(node('p',`${phaseNames[phase]}：${labels[stage.status]}${stage.error ? ' · ' + stage.error : ''}`,'hint'));
  }
  async function request(method, token) {
    const sid = state.sid;
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(sid)}/report-v2`,{method}), view = await response.json();
      if (token !== state.token || sid !== state.sid) return;
      if (!response.ok) throw new Error(view.detail || '无法读取报告，请重试。');
      render(view);
      if (view.status === 'generating') state.timer = setTimeout(() => request('GET',token),800);
    } catch (error) {
      if (token !== state.token || sid !== state.sid) return;
      const message = /[\u4e00-\u9fff]/.test(error.message) ? error.message : '无法连接或读取本机报告服务，请重试。';
      el('reportStatus').textContent = message + ' 基础结算保留。';
      el('reportGenerate').disabled = false; el('reportGenerate').textContent = '重新查看或重试';
    }
  }
  function finished(data) {
    reset(); state.sid = data.session_id; el('reportV2').classList.toggle('hidden', location.pathname === '/models');
    if (data.settlement) settlement(data.settlement);
    el('reportStatus').textContent = '基础结算已就绪，正在读取本次报告。'; el('reportGenerate').disabled = true;
    request('GET',state.token);
  }
  el('reportGenerate').onclick = () => { clearTimeout(state.timer); el('reportGenerate').disabled = true; request('POST',state.token); };
  el('reportReload').onclick = () => { clearTimeout(state.timer); request('GET',state.token); };
  el('reportDownload').onclick = () => {
    if (!state.view?.markdown) return;
    const url = URL.createObjectURL(new Blob([state.view.markdown],{type:'text/markdown;charset=utf-8'}));
    const link = node('a'); link.href = url; link.download = `试讲报告V2-${state.sid}.md`; link.click();
    setTimeout(() => URL.revokeObjectURL(url),1000);
  };
  window.reportV2 = {reset,finished,locate,get hasReport() { return Boolean(state.sid); }};
})();
