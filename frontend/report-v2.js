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
    for (const id of ['reportSummary','reportAnalysis','reportEvidence','reportStages','reportStudentDetails']) el(id).replaceChildren();
    el('reportDetails').open = false;
    el('reportDownload').disabled = true;
  }
  function settlement(summary, reader) {
    const target = el('reportSummary'); target.replaceChildren();
    const grid = node('div', undefined, 'report-metrics');
    for (const item of Object.values(reader?.metrics || summary.metrics)) {
      const card = node('div', undefined, 'report-metric');
      card.append(node('span', item.label), node('strong', item.display || (item.value === null ? '无法统计' : String(item.value))));
      const method = node('details'); method.append(node('summary','统计口径'), node('p',item.method,'hint'));
      if (item.state === 'not_occurred') method.append(node('p','记录中的次数：' + item.value,'hint'));
      card.append(method); grid.append(card);
    }
    target.append(grid, node('p', summary.incomplete ? '反馈可能不完整：存在未处理讲授或录音识别缺口，不能算作学生已经学到。请核对是否已重讲。' : '课堂已完成收尾。当前理解是课堂推断，不是独立验证。', summary.incomplete ? 'report-warning' : 'hint'));
    target.append(node('h3','这次实际涉及的知识'));
    const statuses = {understood:'当前理解', tentative:'暂定理解', conflict:'存在冲突', unclear:'信息缺失'};
    for (const point of summary.knowledge_points) target.append(node('p',`${point.text} · ${statuses[point.status] || point.status}`));
    if (!summary.knowledge_points.length) target.append(node('p','没有可靠的已处理知识记录。','hint'));
    target.append(node('h3','疑问与处理情况'));
    const qlabels = {pending:'系统待提问', asked:'系统已提问', resolved:'系统标记已解决', deferred:'暂缓处理，尚未解决'};
    for (const q of summary.questions) target.append(node('p',`${q.text} · ${q.expressed ? '学生已提出' : '仅系统记录，未确认学生提出'} · ${qlabels[q.status] || q.status} · 回应来源：${q.response_sources?.join('、') || '未记录'} · 内容解释来源：${q.explanation_sources?.join('、') || '未记录'} · 独立理解：尚未验证`));
    for (const note of summary.limitations || []) target.append(node('p',note,'hint'));
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
    for (let parent=row.parentElement; parent; parent=parent.parentElement) if (parent.tagName === 'DETAILS') parent.open=true;
    const body = row.querySelector('[data-evidence-text]'), text = state.view.snapshot.evidence[id].text;
    const quotes = Array.isArray(quote) ? quote : [quote];
    const ranges = quotes.filter(Boolean).map(q => [text.indexOf(q), text.indexOf(q)+q.length]).filter(r => r[0]>=0).sort((a,b)=>a[0]-b[0]);
    const merged=[];
    for (const range of ranges) {
      const last=merged[merged.length-1];
      if (last && range[0]<=last[1]) last[1]=Math.max(last[1],range[1]); else merged.push(range);
    }
    body.replaceChildren(); let end=0;
    for (const range of merged) { body.append(document.createTextNode(text.slice(end,range[0])),node('mark',text.slice(...range))); end=range[1]; }
    body.append(document.createTextNode(text.slice(end)));
    row.scrollIntoView({behavior:'smooth',block:'center'}); row.focus();
  }
  function citations(parent, items) {
    const refs = node('div',undefined,'report-citations');
    const grouped = new Map();
    for (const c of items || []) { if (!grouped.has(c.event_id)) grouped.set(c.event_id,new Set()); grouped.get(c.event_id).add(c.quote); }
    for (const [id,quotes] of grouped) {
      const source=state.view?.snapshot.evidence[id];
      const speaker=source?.kind === 'reply' ? 'AI 学生' : '教师';
      const at=source?.at == null ? '记录 '+source?.order : source.at+' 秒';
      const button = node('button','查看原话 · '+speaker+' · '+at,'small'); button.type = 'button';
      button.setAttribute('data-event-id',id);
      button.onclick = () => locate(id,[...quotes]); refs.append(button);
    }
    parent.append(refs);
  }
  function section(title, target=el('reportAnalysis')) {
    const n = node('section',undefined,'report-section'); n.append(node('h3',title)); target.append(n); return n;
  }
  function analysis(view) {
    el('reportAnalysis').replaceChildren();
    el('reportStudentDetails').replaceChildren();
    const reader = view.reader;
    const overall = section('课堂总体评价');
    overall.append(node('p',reader?.summary || (view.diagnosis ? '旧版报告未生成总体评价；请查阅下列逐项评价。' : '教学分析尚未完成，不能据此判断没有教学问题。')));
    citations(overall,reader?.summary_citations);
    if (view.stages?.diagnosis?.status === 'failed') {
      const error=view.stages.diagnosis.error;
      overall.append(node('p','教学分析生成失败：' + (error.includes('结构') ? '模型输出不符合报告结构要求，具体错误见详细依据。' : error),'report-warning'));
      const retry=node('button','重试未完成分析'); retry.type='button'; retry.onclick=()=>el('reportGenerate').click(); overall.append(retry);
    }
    if (view.diagnosis) {
      const strengths=section('本次试讲的优点');
      for (const finding of reader?.strengths || view.diagnosis.strengths) {
        const block=node('div',undefined,'report-item'); block.append(node('p',finding.observation),node('p','教学作用（分析）：'+finding.interpretation,'hint'));
        citations(block,finding.citations); strengths.append(block);
      }
      if (!view.diagnosis.strengths.length) strengths.append(node('p','现有依据不足以提炼明确优点。'));
      const issues=section('最值得改进的两个问题（本次试讲的不足）');
      const findings=reader?.issues || view.diagnosis.weaknesses.slice(0,2).map(f=>({...f,suggestion:view.diagnosis.suggestions.find(s=>s.finding_id===f.id)}));
      for (const [i,finding] of findings.entries()) {
        const block=node('div',undefined,'report-item');
        block.append(node('h4',`${i+1}. ${finding.observation}`),node('p',finding.interpretation),node('p','下次应该怎么改：'+(finding.suggestion?.action || '尚未生成有依据的具体动作，不能用套话补位。')));
        citations(block,finding.citations); issues.append(block);
      }
      if (!findings.length) issues.append(node('p','未发现证据充分的主要改进问题；这不代表所有教学维度均已验证。'));
    }
    const understanding=section('学生理解情况');
    understanding.append(node('p',reader?.student || '课堂学生与课后作答均为 AI 模拟，没有真实学生理解证据。'));
    const counts=reader?.verification_counts || {};
    understanding.append(node('p',Object.keys(counts).length ? '课后 AI 模拟检查：'+Object.entries(counts).map(([k,v])=>`${k} ${v} 题`).join('；')+'。' : '课后 AI 模拟检查尚未完成或没有可靠题目。'));
    const recall = section('学生说，我学到了什么',el('reportStudentDetails'));
    recall.append(node('p','这是模拟学生的表达，不能仅凭复述就认为已经理解。','hint'));
    if (!view.recall) recall.append(node('p','尚未生成或生成失败，暂不展示复述。'));
    else for (const [key,title] of [['explained','我的理解'],['doubts','我实际表达过的疑问'],['uncertain','尚未确认的理解（模拟推断）']]) {
      recall.append(node('h4',title));
      for (const item of view.recall[key]) { const block = node('div',undefined,'report-item'); block.append(node('p',item.text)); citations(block,item.citations); recall.append(block); }
      if (!view.recall[key].length) recall.append(node('p','暂无有依据的内容。','hint'));
    }
    const verification = section('学生真的理解了吗',el('reportStudentDetails'));
    verification.append(node('p','模拟验证只提供课堂内解释或应用的证据，不能证明真实掌握或知识客观正确。','hint'));
    const answers = new Map((view.answers?.answers || []).map(a => [a.id,a]));
    const results = new Map((view.verification?.results || []).map(r => [r.id,r]));
    for (const probe of view.recall?.probes || []) {
      const block = node('div',undefined,'report-item'), answer = answers.get(probe.id), result = results.get(probe.id);
      block.append(node('h4',probe.question), node('p','学生回答：' + (answer?.text || '尚未验证')), node('strong',result?.status || '尚未验证'), node('p',result?.explanation || '验证尚未完成或失败。'));
      citations(block,[...probe.citations,...(answer?.citations || []),...(result?.citations || [])]); verification.append(block);
    }
    if (!view.recall?.probes.length) verification.append(node('p','尚无可靠验证题，不推断已经理解。'));
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
    state.view = view; settlement(view.snapshot.settlement,view.reader); analysis(view); evidence(view.snapshot);
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
  // Reopen one persisted report via its ID; this only performs GET, never regeneration.
  const savedReport=new URLSearchParams(location.search).get('report');
  if (savedReport && /^[a-f0-9]{12}$/.test(savedReport)) {
    finished({session_id:savedReport}); el('reportV2').scrollIntoView({block:'start'});
  }
})();
