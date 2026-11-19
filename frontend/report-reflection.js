'use strict';
(() => {
  function node(tag, text, className) {
    const element = document.createElement(tag);
    if (text) element.textContent = text;
    if (className) element.className = className;
    return element;
  }

  function line(label, text) {
    const element = node('p');
    element.append(node('strong', label), document.createTextNode('　' + text));
    return element;
  }

  window.renderReportReflection = (report, {showMascot = false} = {}) => {
    const root = node('div', null, 'reflection-report');
    const state = node('section', null, 'reflection-state');
    const copy = node('div');
    copy.append(node('h2', '本次状态'), node('p', report.overview.text));
    if (report.keep) copy.append(line('值得保留', report.keep.text));
    state.append(copy);
    if (showMascot) {
      const mascot = node('img', null, 'reflection-mascot');
      mascot.src = '/static/formal/assets/小纸-用户原图.png';
      mascot.alt = '';
      state.append(mascot);
    }
    root.append(state);

    const body = node('div', null, 'reflection-body');
    const notes = node('div', null, 'reflection-notes');
    if (report.findings.length) {
      const changes = node('section', null, 'reflection-panel');
      changes.append(node('h2', '改进建议'));
      for (const finding of report.findings) {
        const issue = node('article', null, 'reflection-finding');
        const heading = node('h3');
        heading.append(node('span', finding.kind, 'reflection-kind'), document.createTextNode(finding.title));
        issue.append(heading, line('课堂依据', finding.fact), line('下次做法', finding.action));
        changes.append(issue);
      }
      notes.append(changes);
    }
    if (report.expression) {
      const speech = node('section', null, 'reflection-panel');
      const item = report.expression;
      speech.append(node('h2', '表达观察'), node('p', item.text),
        line('转写片段', '“' + item.quote + '”'), line('可以这样讲', item.action));
      notes.append(speech);
    }
    if (notes.childElementCount) body.append(notes);
    if (report.practice) {
      const practice = node('aside', null, 'reflection-panel reflection-practice');
      const item = report.practice;
      practice.append(node('h2', '下次试试'), node('p', `${item.minutes}分钟练习　${item.task}`),
        node('p', item.example, 'reflection-example'), line('完成标准', item.criterion));
      body.append(practice);
    }
    if (body.childElementCount) root.append(body);
    root.append(node('p', '报告依据课堂文字和小纸的模拟回应。', 'reflection-scope'));
    return root;
  };
})();
