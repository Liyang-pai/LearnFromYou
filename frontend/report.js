'use strict';
(() => {
  let completedId = '', busy = false, generation = 0, result = null;
  function sync() {
    $('generateReport').disabled = busy || !completedId;
    $('downloadReport').disabled = busy || !result;
    $('generateReport').textContent = busy ? '报告生成中…' : result ? '查看已生成报告' : '生成试讲报告';
  }
  function reset() {
    generation += 1; completedId = ''; busy = false; result = null;
    $('trialReport').classList.add('hidden');
    $('reportContent').replaceChildren();
    $('reportStatus').textContent = '结束课堂后可生成';
    $('reportError').textContent = '';
    sync();
  }
  function show(report) {
    const holder = $('reportContent'); holder.replaceChildren();
    const facts = report.facts;
    holder.append(recordNode('本次课堂概况', `${facts.topic} · 讲授片段 ${facts.teacher_segments} · 学生回答 ${facts.student_replies} · 独立测验 ${facts.assessment_count}`),
      recordNode('计划讲授（不等于实际讲授）', facts.planned_points || '未填写'));
    for (const note of report.limitations) holder.append(recordNode('报告边界', note));
    for (const question of facts.unresolved_questions) holder.append(recordNode(`未解决疑问 · ${question.id}`, question.text));
    for (const [title, key] of [['实际讲授','taught_points'],['学生理解表现','understanding'],['认知变化','changes'],['下一次优先复查与改进','priorities']]) {
      const heading = document.createElement('h3'); heading.textContent = title; holder.append(heading);
      const findings = report.analysis[key];
      if (!findings.length) holder.append(recordNode('证据不足', '本次没有足够证据生成该部分结论。'));
      for (const item of findings) {
        const card = document.createElement('article'); card.className = 'report-finding';
        card.append(recordNode(item.observation, item.interpretation),
          recordNode('依据类型', item.basis === 'assessment' ? '独立测验表现' : '课堂观察与推断'));
        if (item.suggestion) card.append(recordNode('下一次可以尝试', item.suggestion));
        for (const citation of item.citations) card.append(recordNode(`课堂证据 · ${citation.source_id}`, citation.quote));
        holder.append(card);
      }
    }
  }
  $('generateReport').onclick = async () => {
    if (busy || !completedId) return;
    if (result) { show(result); return; }
    const current = generation, id = completedId;
    busy = true; sync(); $('reportError').textContent = '';
    $('reportStatus').textContent = '正在分析课堂证据，请稍候';
    try {
      const response = await fetch(`/api/sessions/${encodeURIComponent(id)}/report`, {method:'POST'});
      const data = await response.json();
      if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '报告生成失败，请重试');
      if (current !== generation) return;
      result = data; show(data);
      $('reportStatus').textContent = data.cached ? '已读取保存的报告' : '报告已生成';
    } catch (error) {
      if (current !== generation) return;
      $('reportStatus').textContent = '未生成，可重试';
      $('reportError').textContent = error.message;
    } finally {
      if (current === generation) { busy = false; sync(); }
    }
  };
  $('downloadReport').onclick = () => {
    if (!result) return;
    const url = URL.createObjectURL(new Blob([result.markdown], {type:'text/markdown;charset=utf-8'}));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `试讲报告-${result.session_id}.md`;
    anchor.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  window.trialReport = {reset, handle(event) {
    if (event.type !== 'finished') return;
    generation += 1; completedId = event.data.session_id; busy = false; result = null;
    $('trialReport').classList.remove('hidden'); $('reportContent').replaceChildren(); $('reportError').textContent = '';
    $('reportStatus').textContent = '课堂已结束，可生成报告'; sync();
  }};
  sync();
})();
