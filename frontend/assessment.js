'use strict';
(() => {
  let busy = false, available = false;
  const results = [];
  const verdicts = {passed:'通过', partial:'部分通过', failed:'未通过', review:'待确认'};
  const coverage = {sufficient:'规则已有课堂依据', missing:'缺少必要讲解', contradictory:'课堂说法矛盾', out_of_scope:'题目超出范围', uncertain:'课堂依据待确认'};
  const kinds = {apply:'应用题', explain:'复述题', boundary:'边界题'};

  function sync() {
    const kind = $('assessmentKind').value;
    const count = results.filter(r => r.question.kind === kind).length;
    $('assess').disabled = !running || ending || recording || micStarting || busy || !available;
    $('assessmentKind').disabled = busy || ending;
    $('assess').textContent = busy ? '测验进行中…' : count ? '补讲后自动出新题' : '检验学生理解';
  }
  function reset() {
    busy = false; available = false; results.length = 0;
    $('assessmentStatus').textContent = '尚未测验';
    $('assessmentQuestion').classList.add('hidden');
    $('assessmentComparison').classList.add('hidden');
    $('assessmentResults').replaceChildren();
    $('assessmentHint').textContent = '根据实际讲授自动出题，无需准备题库。先暂停麦克风，等待认知更新后开始。';
    sync();
  }
  function showResult(result) {
    const previous = [...results].reverse().find(r => r.question.kind === result.question.kind);
    results.push(result);
    const card = document.createElement('article'); card.className = 'assessment-result';
    const title = document.createElement('h3');
    title.textContent = `${kinds[result.question.kind]} · 第 ${result.round} 次 · ${verdicts[result.evaluation.verdict]}`;
    card.append(title, recordNode('题目', result.question.text), recordNode('学生作答', result.answer.text));
    if (result.answer.uncertainty) card.append(recordNode('学生尚不确定', result.answer.uncertainty));
    card.append(recordNode(coverage[result.evaluation.coverage], result.evaluation.explanation));
    if (result.evaluation.gap) card.append(recordNode('理解或讲解缺口', result.evaluation.gap));
    if (result.evaluation.suggestion) card.append(recordNode('下一次练习', result.evaluation.suggestion));
    for (const evidence of result.evaluation.evidence) card.append(recordNode(`课堂原文 · ${evidence.source_id}`, evidence.quote));
    if (!results.slice(0,-1).length) $('assessmentResults').replaceChildren();
    $('assessmentResults').prepend(card);
    $('assessmentStatus').textContent = verdicts[result.evaluation.verdict];
    if (previous) {
      $('assessmentComparison').textContent = `${kinds[result.question.kind]}：第 ${previous.round} 次 ${verdicts[previous.evaluation.verdict]} → 第 ${result.round} 次 ${verdicts[result.evaluation.verdict]}。不同情境，课堂版本 ${previous.state_version} → ${result.state_version}；题目可比性仍需人工复核。`;
      $('assessmentComparison').classList.remove('hidden');
    }
  }
  function handle(event) {
    const data = event.data || {};
    if (event.type === 'ready') {
      available = data.assessment_available === true;
      $('assessmentHint').textContent = available ? '所有主题均可根据实际讲授自动出题。补讲后围绕同一知识点换题再测；内容不足会提示补充。' : '当前服务尚不支持自动测验，请更新并重启服务。';
    } else if (event.type === 'assessment_status') {
      busy = data.busy;
      if (data.busy) {
        $('assessmentStatus').textContent = data.question ? '独立作答与评估中' : '自动出题与检查中';
        if (data.question) {
          $('assessmentQuestion').textContent = data.question.text;
          $('assessmentQuestion').classList.remove('hidden');
        } else $('assessmentQuestion').classList.add('hidden');
      }
    } else if (event.type === 'assessment_result') {
      showResult(data);
    } else if (event.type === 'assessment_discarded') {
      $('assessmentStatus').textContent = '课堂已变化，请重试';
      showError(data.message);
    } else if (event.type === 'error' && data.code === 'assessment_failure') {
      busy = false;
      $('assessmentStatus').textContent = '本题未提交，可重试';
    } else if (event.type === 'finished') {
      busy = false;
    }
    syncControls();
  }
  window.assessment = {get busy() { return busy; }, sync, reset, handle,
    disconnected() { busy = false; sync(); }};
  $('assessmentKind').onchange = sync;
  $('assess').onclick = () => {
    if ($('assess').disabled || socket?.readyState !== WebSocket.OPEN) return;
    busy = true; syncControls();
    send({type:'assessment', kind:$('assessmentKind').value});
  };
  sync();
})();
