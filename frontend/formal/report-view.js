import {act, escape, html, link, setText, shell, template} from './prototype.js';
import {time} from './records-view.js';

const axes = [
  ['accuracy', '内容准确', 185, 23, ''],
  ['clarity', '讲解清楚', 310, 106, ' report-axis-right'],
  ['coherence', '结构连贯', 263, 225, ' report-axis-right report-axis-lower'],
  ['interaction', '师生互动', 107, 225, ' report-axis-left report-axis-lower'],
  ['expression', '表达规范', 60, 106, ' report-axis-left'],
];
const levels = ['起步', '部分做到', '基本达标', '表现良好', '表现充分'];
const speaker = {teacher: '教师', ai: '小纸'};
const iconPaths = {
  book: '<rect x="5" y="4" width="14" height="16" rx="2"/><path d="M7 13h10M8 8h7"/>',
  chart: '<path d="M4 19h16M7 15v-4m5 4V6m5 9V9"/>',
  chevron: '<path d="m6 9 6 6 6-6"/>',
};
const icon = (name, className = 'icon') => `<svg class="${className}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${iconPaths[name]}</svg>`;
const timestamp = value => Number.isFinite(value) && value >= 0 ? `<time>${time(value)}</time>` : '';

function transcriptRow(row) {
  const label = speaker[row.role];
  const original = row.original_text && row.original_text !== row.text
    ? `<details class="report-original"><summary>查看识别原文</summary><p>${escape(row.original_text)}</p></details>` : '';
  return `<article class="report-turn" data-turn-id="${escape(row.id)}" tabindex="-1" aria-label="${label}">
    <div class="report-turn-head">${timestamp(row.elapsed)}<span class="badge${row.role === 'ai' ? ' gray' : ''}">${label}</span></div>
    <p data-turn-text>${escape(row.text)}</p>${original}
  </article>`;
}

function evidenceList(evidence, transcript) {
  if (!evidence?.length) return '';
  return `<div class="report-evidence-list">${evidence.map(item => {
    const turn = transcript.get(item.source_id);
    const role = speaker[item.actor] || '课堂原文';
    const quote = `<q>${escape(item.quote)}</q>`;
    if (!turn || turn.role !== item.actor) return `<div class="report-evidence"><span>${role}</span>${quote}</div>`;
    return `<button class="report-evidence" type="button" data-action="report-jump" data-id="${escape(turn.id)}" data-quote="${escape(item.quote)}">
      <span><strong>${role}</strong>${timestamp(turn.elapsed)}</span>${quote}</button>`;
  }).join('')}</div>`;
}

function wordChart(counts, expression, transcript) {
  const rows = Array.isArray(counts) ? counts.filter(row => Number.isSafeInteger(row?.count) && row.count >= 2).slice(0, 4) : [];
  const max = Math.max(1, ...rows.map(row => row.count));
  return `<section class="report-stat" aria-labelledby="words-title">
    <h3 id="words-title">口头禅统计</h3><p class="report-stat-note">基于教师语音转写</p>
    ${rows.length ? rows.map(({word, count}) => `<div class="report-word"><span>${escape(word)}</span><span class="report-bar" aria-hidden="true"><i style="width:${count / max * 100}%"></i></span><span>${count} 次</span></div>`).join('')
      : `<p class="report-stat-note">${Array.isArray(counts) ? '未统计到出现至少 2 次的口头禅。' : '暂无口头禅统计。'}</p>`}
    ${expression ? `<details class="report-expression"><summary>表达观察</summary><p>${escape(expression.text)}</p>${evidenceList(expression.evidence, transcript) || (expression.quote ? `<p><q>${escape(expression.quote)}</q></p>` : '')}<p>${escape(expression.action)}</p></details>` : ''}
  </section>`;
}

const accuracyBasisLabel = check => check.verdict === 'unverified' ? '待核实原因' : '学科依据';

function accuracyPreview(check, transcript, count, reason) {
  const evidence = check.evidence?.findLast(item => item.actor === 'teacher');
  const turn = evidence && transcript.get(evidence.source_id);
  return `<div id="report-reason-accuracy">
    <p><strong>${escape(check.topic)} · ${escape(check.label)}</strong></p>
    <p><strong class="report-accuracy-label">课堂原话</strong>${timestamp(turn?.elapsed)}<br>${evidence ? `<q>${escape(evidence.quote)}</q>` : '未能定位课堂原话。'}</p>
    <p><strong class="report-accuracy-label">${accuracyBasisLabel(check)}</strong><br>${escape(check.basis)}</p>
    ${reason ? `<p>${escape(reason)}</p>` : ''}
  </div><p class="report-stat-note">点击分数查看全部 ${count} 项</p>`;
}

function radar(scores, transcript) {
  const centerX = 185, centerY = 132, radius = 77;
  const point = (index, level) => {
    const angle = Math.PI * 2 * index / axes.length;
    const distance = radius * level / 5;
    return [+(centerX + Math.sin(angle) * distance).toFixed(2), +(centerY - Math.cos(angle) * distance).toFixed(2)];
  };
  const rings = levels.map((_, i) => `<polygon points="${axes.map((_, index) => point(index, i + 1)).join(' ')}"/>`).join('');
  const spokes = axes.map((_, index) => {
    const [x, y] = point(index, 5);
    return `<line x1="${centerX}" y1="${centerY}" x2="${x}" y2="${y}"/>`;
  }).join('');
  const vertices = [];
  const labels = axes.map(([key, name, x, y, position], i) => {
    const item = scores?.[key], score = item?.score;
    const valid = Number.isFinite(score) && score >= 1 && score <= 5;
    const hasChecks = key === 'accuracy' && Array.isArray(item?.checks);
    const pending = item?.pending_label || (item ? '证据不足' : '待评分');
    const value = valid ? score.toFixed(1) : pending;
    const grade = valid ? levels[Math.floor(Number(value)) - 1] : pending;
    const reason = item?.reason || (item ? '现有课堂片段还不足以给这一项定档。' : '这份报告尚未生成五维评分。');
    const preview = hasChecks ? item.checks[item.preview_check_index] : null;
    vertices.push(valid ? point(i, Number(value)) : null);
    return `<div class="report-axis${position}" data-dimension="${key}" style="--axis-x:${x / 370 * 100}%;--axis-y:${y / 250 * 100}%">
      <button type="button" aria-label="${name}，${valid ? value + ' 分，' : ''}${escape(preview && valid ? '查看核对依据' : grade)}" aria-describedby="report-reason-${key}"
        ${hasChecks ? 'data-action="report-accuracy" aria-controls="report-accuracy-details" aria-expanded="false"' : ''}>
        <strong>${name}</strong><span class="${valid ? 'report-score' : 'report-score-pending'}">${escape(value)}</span></button>
      <div class="report-axis-tip${preview ? ' report-axis-tip-accuracy' : ''}">
        ${preview ? accuracyPreview(preview, transcript, item.checks.length, !valid || score === 1 ? reason : '')
          : `<p id="report-reason-${key}"><strong>${escape(grade)}</strong><br>${escape(reason)}</p>${evidenceList(item?.evidence, transcript)}`}
      </div>
    </div>`;
  }).join('');
  let shape = '';
  if (vertices.every(Boolean)) {
    shape = `<polygon class="report-radar-shape" points="${vertices.join(' ')}"/>`;
  } else {
    const sectors = [], edges = [];
    vertices.forEach((from, i) => {
      const to = vertices[(i + 1) % vertices.length];
      if (!from || !to) return;
      sectors.push(`M${centerX},${centerY} L${from} L${to} Z`);
      edges.push(`M${from} L${to}`);
    });
    if (edges.length) {
      shape = `<path class="report-radar-partial-fill" d="${sectors.join(' ')}"/>
        <path class="report-radar-partial-line" d="${edges.join(' ')}"/>`;
    }
  }
  return `<section class="report-stat" aria-labelledby="dimensions-title">
    <div class="report-score-head"><h3 id="dimensions-title">五维评价</h3><span>1.0–5.0 分</span></div>
    <div class="report-radar" role="group" aria-label="五维雷达图">
      <svg viewBox="0 0 370 250" aria-hidden="true" focusable="false">
        <g class="report-radar-grid">${rings}${spokes}</g>
        ${shape}
        ${vertices.filter(Boolean).map(([x, y]) => `<circle class="report-radar-dot" cx="${x}" cy="${y}" r="4.2"/>`).join('')}
      </svg>${labels}
    </div>
  </section>`;
}

function accuracyDetails(item, transcript) {
  if (!Array.isArray(item?.checks)) return '';
  return `<section class="report-stat report-accuracy-details" id="report-accuracy-details" aria-labelledby="accuracy-title" hidden>
    <h3 id="accuracy-title">内容准确核对</h3>
    <p class="report-stat-note">${escape(item.reason)}</p>
    ${item.checks.map(check => `<article class="report-accuracy-check">
      <span class="report-stat-note">${check.core ? '核心' : '次要'} · ${escape(check.label)}</span>
      <p><strong>${escape(check.claim)}</strong></p>
      <div class="report-accuracy-basis">
        <div>
          <h4 class="report-accuracy-label">课堂原话</h4>
          ${evidenceList(check.evidence, transcript) || '<p class="report-stat-note">未能定位课堂原话。</p>'}
        </div>
        <div class="report-accuracy-grounds">
          <h4 class="report-accuracy-label">${accuracyBasisLabel(check)}</h4>
          <p>${escape(check.basis)}</p>
        </div>
      </div>
    </article>`).join('')}
  </section>`;
}

function findingsList(findings, transcript) {
  return `<section class="report-issues" aria-labelledby="improvements-title">
    <h3 id="improvements-title">重点改进 <span class="badge">${findings.length} 项</span></h3>
    ${findings.map((item, index) => `<details class="report-issue" name="report-issues"${index === 0 ? ' open' : ''}>
      <summary class="report-summary"><strong>${escape(item.title)}</strong><span class="badge">${escape(item.kind)}</span>${icon('chevron', 'report-chevron')}</summary>
      <div class="report-issue-content">
        <div><strong>课堂证据</strong>${evidenceList(item.evidence, transcript)}<p>${escape(item.fact)}</p></div>
        <div class="report-suggestion"><strong>下次做法</strong><p>${escape(item.action)}</p></div>
      </div>
    </details>`).join('') || '<p class="report-stat-note">本次没有单列重点改进。</p>'}
  </section>`;
}

function reportLayout(result) {
  const {report, transcript, word_counts, scores} = result;
  const turns = (transcript || []).filter(row => row.role === 'teacher' || row.role === 'ai');
  const byId = new Map(turns.map(row => [row.id, row]));
  const practice = report.practice;
  return `<div class="report-grid report-review-grid">
    <section class="card report-review-panel" aria-labelledby="transcript-title">
      <header class="report-review-head"><span class="report-symbol">${icon('book')}</span><h2 id="transcript-title">教学原文（试讲过程）</h2></header>
      <div class="report-scroll report-transcript" id="transcript-scroll" tabindex="0" role="region" aria-label="教学原文列表">
        ${result.transcription_notice ? `<p class="report-stat-note" data-transcription-notice>${escape(result.transcription_notice)}</p>` : ''}
        ${turns.map(transcriptRow).join('') || `<p class="report-stat-note">${Array.isArray(transcript) ? '本次没有课堂发言记录。' : '这份报告暂无课堂原文。'}</p>`}
      </div>
    </section>
    <section class="card report-review-panel" aria-labelledby="analysis-title">
      <header class="report-review-head"><span class="report-symbol">${icon('chart')}</span><div><h2 id="analysis-title">教学分析与诊断</h2><p>从内容、讲解、结构、互动和表达回看本次试讲。</p></div></header>
      <div class="report-scroll report-analysis" id="analysis-scroll" tabindex="0" role="region" aria-label="教学分析内容">
        <section class="report-highlight report-overview" aria-labelledby="overview-title">
          <div><h3 id="overview-title">本次教学概览</h3><p>${escape(report.overview.text)}</p></div>
          <img src="/static/formal/assets/小纸-用户原图.png" alt="" width="71" height="76">
        </section>
        <div class="report-stat-grid">${wordChart(word_counts, report.expression, byId)}${radar(scores, byId)}</div>
        ${accuracyDetails(scores?.accuracy, byId)}
        <details class="report-strengths" open>
          <summary class="report-summary"><strong>教学亮点</strong><span class="badge green">${report.keep ? 1 : 0} 项</span>${icon('chevron', 'report-chevron')}</summary>
          ${report.keep ? `<ul><li>${escape(report.keep.text)}${report.keep.evidence?.length ? `<details class="report-strength-evidence"><summary>课堂证据</summary>${evidenceList(report.keep.evidence, byId)}</details>` : ''}</li></ul>` : '<p class="report-stat-note">本次没有单列教学亮点。</p>'}
        </details>
        ${findingsList(report.findings, byId)}
        <section class="report-practice" aria-labelledby="practice-title">
          <h3 id="practice-title">下次试讲练习建议</h3>
          ${practice ? `<ul><li><strong>${escape(practice.minutes)} 分钟练习</strong>　${escape(practice.task)}</li><li><strong>示范话术</strong>　${escape(practice.example)}</li><li><strong>完成标准</strong>　${escape(practice.criterion)}</li></ul>` : '<p class="report-stat-note">暂无练习建议。</p>'}
          <button class="btn primary" type="button" data-action="home-create">再创建一次试讲</button>
        </section>
      </div>
    </section>
  </div>`;
}

export function jumpToReportEvidence(view, id, quote) {
  const transcript = view.querySelector('#transcript-scroll');
  const turn = [...transcript.querySelectorAll('.report-turn')].find(row => row.dataset.turnId === id);
  if (!turn) return;
  const previous = transcript.querySelector('.report-turn.is-linked');
  previous?.classList.remove('is-linked');
  const previousMark = previous?.querySelector('mark');
  previousMark?.replaceWith(previousMark.textContent);
  turn.classList.add('is-linked');
  const text = turn.querySelector('[data-turn-text]');
  const content = text.textContent;
  const start = quote ? content.indexOf(quote) : -1;
  if (start >= 0) {
    const mark = document.createElement('mark');
    mark.textContent = quote;
    text.replaceChildren(content.slice(0, start), mark, content.slice(start + quote.length));
  }
  turn.scrollIntoView({block: 'center', inline: 'nearest', behavior: 'instant'});
  turn.focus({preventScroll: true});
}

export function report(state, path) {
  const r = state.report, id = path.split('/').pop(), ready = r.status === 'ready' && r.report;
  const busy = r.status === 'generating' || r.loading;
  const root = shell(template(ready ? '20' : busy ? '19' : '21'), '/classroom');
  const lesson = state.session.id === id ? state.session.lesson : {};
  const topic = r.topic || lesson?.topic;
  const duration = Number.isFinite(r.elapsed) && r.elapsed >= 0 ? time(r.elapsed) : '';
  setText(root, '.page-head p', [topic ? '试讲主题：' + topic : '本次课堂', r.subject || lesson?.subject, r.grade || lesson?.grade, duration && '时长：' + duration].filter(Boolean).join('　·　'));
  for (const badge of root.querySelectorAll('.page-head .badge')) {
    badge.textContent = ready ? '报告已生成' : busy ? '正在生成' : r.status === 'failed' ? '生成失败' : '等待生成';
  }
  for (const button of root.querySelectorAll('button')) {
    const text = button.textContent.trim();
    if (text === '导出 Markdown' || text === '导出日志') {
      const url = text === '导出 Markdown' ? `/api/teaching-report/${id}/export` : `/api/history/${id}/log`;
      link(button, url).setAttribute('download', '');
    }
    if (/重新试讲|再来一次|再次试讲|再创建一次试讲/.test(text)) act(button, 'home-create');
    if (/返回|回看|查看课堂记录/.test(text)) act(button, 'report-back', id);
    if (/重试|重新生成/.test(text)) { act(button, 'report-start'); button.disabled = !!r.loading; }
  }
  if (ready) {
    const data = r.report;
    if (Array.isArray(data.findings)) {
      root.querySelector('.main').classList.add('report-review');
      root.querySelector('.report-grid').outerHTML = reportLayout(r);
      return html(root);
    }
    setText(root, '.report-main>h2', '本次教学报告');
    setText(root, '.report-intro', data.overview?.text);
    if (r.transcription_notice) {
      const notice = document.createElement('p');
      notice.className = 'report-stat-note';
      notice.dataset.transcriptionNotice = '';
      notice.textContent = r.transcription_notice;
      root.querySelector('.report-intro')?.after(notice);
    }
    const keys = ['accuracy', 'clarity', 'coherence', 'checking'];
    for (const [index, node] of [...root.querySelectorAll('.dimension')].entries()) {
      const dimension = data.dimensions[keys[index]];
      setText(node, '.badge', dimension.label);
      node.lastElementChild.textContent = dimension.reason;
    }
    const sections = root.querySelectorAll('.report-main .report-section');
    if (sections[1]) {
      const paragraphs = sections[1].querySelectorAll(':scope>p');
      if (paragraphs[0]) paragraphs[0].textContent = data.finding.text;
      if (paragraphs[1]) paragraphs[1].textContent = '值得保留：' + (data.keep?.text || '本次材料不足，暂不单列。');
    }
    setText(root, '.report-highlight p', data.action.text);
    setText(root, '.practice-card .badge', '约 ' + data.practice.minutes + ' 分钟');
    setText(root, '.practice-card .task', data.practice.task);
    const descriptions = root.querySelectorAll('.practice-card>p:not(.task)');
    for (const [index, paragraph] of [...descriptions].entries()) paragraph.textContent = index === 0 ? data.practice.task : data.practice.criterion;
  } else {
    const main = root.querySelector('.report-loading,.report-empty,.card');
    if (main) {
      if (!busy) setText(main, ':scope>p', r.error || '尚未生成报告，点击后才会调用模型。');
      const warning = main.querySelector('.warning-box');
      if (warning) { warning.textContent = r.error || ''; warning.hidden = !r.error; }
      if (!busy && r.status !== 'failed' && !r.error) setText(main, 'h2', '尚未生成教学报告');
      if (!busy) {
        let button = main.querySelector('[data-action="report-start"]');
        if (!button) { button = document.createElement('button'); button.className = 'btn primary'; main.append(button); }
        button.textContent = r.status === 'failed' ? '重试生成报告' : '生成报告';
        act(button, 'report-start');
      }
    }
    const progress = root.querySelector('.progress');
    if (progress) {
      progress.setAttribute('role', 'progressbar');
      progress.setAttribute('aria-label', '报告正在处理');
      progress.querySelector('i')?.removeAttribute('style');
    }
    setText(root, '.report-empty>.hint', busy ? '课堂记录已保存，请稍候。' : r.error || '尚未生成报告。');
  }
  return html(root);
}
