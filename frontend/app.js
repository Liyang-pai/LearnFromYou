'use strict';
const $ = id => document.getElementById(id);
let socket, running = false, starting = false, recording = false, ending = false;
let micGeneration = 0;
let context, stream, source, worklet, processor, silentGain, flushed, fallbackTimer;
let sentAudioFrames = 0;
let micStarting = false, micStopping = false;
let records = [], transcriptCount = 0, segmentCount = 0, sessionId = '';
let glossaryBusy = false, reviewBusy = false, studentBusy = false, studentFailed = false;
const phaseLabels = {preparation:'课前范围分析', review_glossary:'课前术语准备', asr_review:'语音转文字审核', student:'学生认知更新', assessment_question:'课堂自动出题', assessment_audit:'题目依据检查', assessment_answer:'测验独立作答', assessment_evaluation:'测验独立评估'};
const reviewLabels = {reviewed:'已审核', fallback:'审核失败 · 使用原文', bypassed:'文字输入 · 无需审核', disabled:'审核已关闭'};
let asrReady = false, asrBusy = false, asrDownloading = false;
let asrDebugAvailable = null;
let audioUploadAvailable = null;
const labels = {tentative:'暂定理解',understood:'课堂推断理解',unclear:'信息缺失',conflict:'冲突或失效',pending:'待提问',asked:'已提问',resolved:'已解决',deferred:'暂缓'};

function send(data) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data)); }
function showError(message, retry = false) {
  $('errorText').textContent = message; $('error').classList.remove('hidden'); $('retry').classList.toggle('hidden', !retry);
}
function syncControls() {
  const debugBusy = Boolean(window.asrDebug?.busy || window.asrDebug?.upload?.busy);
  const uploadBusy = Boolean(window.lessonUpload?.busy);
  $('start').disabled = running || starting || !asrReady || asrBusy || asrDownloading || debugBusy;
  $('changeModel').disabled = running || starting || ending || asrBusy || debugBusy;
  for (const id of ['mic','stopSpeech','end','teacherText','sendText']) $(id).disabled = !running || ending;
  $('mic').disabled ||= uploadBusy || micStarting || micStopping;
  for (const id of ['teacherText','sendText']) $(id).disabled ||= uploadBusy;
  for (const id of ['topic','points','prerequisites','level','asrReview']) $(id).disabled = running || starting || ending;
  $('mic').textContent = recording ? '暂停麦克风' : '开启麦克风';
  if (window.assessment?.busy || micStarting) $('mic').disabled = true;
  window.assessment?.sync();
  window.asrDebug?.sync();
  window.lessonUpload?.sync();
}
function append(container, node, first = false) {
  if (container.classList.contains('empty')) { container.textContent = ''; container.classList.remove('empty'); }
  first ? container.prepend(node) : container.append(node);
  while (container.childElementCount > 200) first ? container.lastElementChild.remove() : container.firstElementChild.remove();
}
function recordNode(title, text, details = false) {
  const node = document.createElement('div'); node.className = 'record';
  if (details) {
    const detail = document.createElement('details'), summary = document.createElement('summary'), pre = document.createElement('pre');
    summary.textContent = title; pre.textContent = text; detail.append(summary, pre); node.append(detail);
  } else {
    const label = document.createElement('small'), body = document.createElement('div'); label.textContent = title; body.textContent = text; node.append(label, body);
  }
  return node;
}
function bubble(text, teacher = false) {
  $('emptyConversation')?.remove();
  const node = document.createElement('div'); node.className = 'bubble' + (teacher ? ' teacher' : '');
  const label = document.createElement('small'); label.textContent = teacher ? '教师 · 文字测试' : '学生';
  const body = document.createElement('div'); body.textContent = text; node.append(label, body); $('conversation').append(node);
  $('conversation').scrollTop = $('conversation').scrollHeight;
}
function showState(state) {
  $('stateVersion').textContent = state.version;
  $('stateJson').textContent = JSON.stringify(state, null, 2);
  const holder = $('stateCards'); holder.textContent = ''; holder.classList.remove('empty');
  for (const [name, items] of [['当前认知',state.knowledge],['问题与解答',state.open_questions ?? state.questions ?? []],['近期课堂事件',state.recent_events ?? []]]) {
    const heading = document.createElement('div'); heading.className = 'state-title'; heading.textContent = name; holder.append(heading);
    if (!items.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = '暂无记录'; holder.append(p); }
    for (const item of items) {
      const row = document.createElement('div'); row.className = 'state-item ' + item.status;
      const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = labels[item.status] || '课堂事件';
      const text = document.createElement('span'); text.textContent = item.text;
      const meta = document.createElement('small'); meta.textContent = `${item.id} · 来源 ${item.sources.join('、')}${item.attempts !== undefined ? ' · 已提问 ' + item.attempts + ' 次' : ''}${item.resolution_sources?.length ? ' · 解答来源 ' + item.resolution_sources.join('、') : ''}`;
      row.append(tag,text,meta); holder.append(row);
    }
  }
}
function showActivity() {
  // Audio frames must not hide a pending review or student request.
  if (ending) $('sessionStatus').textContent = '正在处理最后的课堂内容';
  else if (studentFailed) $('sessionStatus').textContent = '学生处理暂停 · 可以重试';
  else if (glossaryBusy) $('sessionStatus').textContent = '正在准备术语';
  else if (reviewBusy && studentBusy) $('sessionStatus').textContent = '正在审核转写 · 学生正在消化';
  else if (reviewBusy) $('sessionStatus').textContent = '正在审核转写';
  else if (studentBusy) $('sessionStatus').textContent = '学生正在消化';
}
function handle(event) {
  records.push(event); const d = event.data || {}, stamp = event.elapsed === undefined ? '' : `${event.elapsed.toFixed(1)}s`;
  window.lessonUpload?.handle(event);
  window.assessment?.handle(event);
  switch (event.type) {
    case 'ready':
      $('asrReview').checked = d.asr_review !== false;
      starting = false; running = true; sessionId = d.session_id; $('sessionStatus').textContent = '课堂已就绪'; $('studentMood').textContent = '这节课的内容，我准备开始听了。';
      $('scope').classList.remove('hidden'); $('scopeTags').textContent = '';
      for (const name of d.scope.scope) { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = name; $('scopeTags').append(tag); }
      $('boundary').textContent = d.scope.boundary_note; showState(d.state); syncControls(); break;
    case 'status': $('sessionStatus').textContent = d.message; break;
    case 'audio':
      if (!d.recording) { micStopping = false; syncControls(); }
      $('sessionStatus').textContent = d.input_source === 'audio_file' ? '正在处理上传音频' : d.recording ? '正在听课' : '音频输入已暂停'; break;
    case 'upload_finished': $('sessionStatus').textContent = '音频识别完成 · 等待课堂后续处理'; break;
    case 'audio_input': $('sessionStatus').textContent = `正在听课 · 已收到 ${d.frames} 个音频帧`; break;
    case 'vad': $('avatar').classList.toggle('listening',d.speaking); $('studentMood').textContent = d.speaking ? '正在听你讲……' : '正在整理刚才听到的内容。'; break;
    case 'transcript':
      $('transcriptCount').textContent = ++transcriptCount;
      append($('transcripts'),recordNode(`${d.id} · ${d.input_source === 'audio_file' ? '上传音频 · 本机 ASR · ' + d.filename : d.mode === 'microphone' ? '本机 ASR' : '文字测试'} · ${stamp}${d.asr_seconds !== undefined ? ' · 识别 ' + d.asr_seconds + 's' : ''}`,d.text));
      $('transcripts').parentElement.scrollTop = $('transcripts').parentElement.scrollHeight; break;
    case 'segment': {
      $('segmentCount').textContent = ++segmentCount;
      const row = recordNode(`教学片段 · ${d.sources.join('、')} · ${d.reason}`,d.text); row.classList.add('segment'); append($('transcripts'),row); break;
    }
    case 'review_glossary':
      glossaryBusy = d.status === 'started';
      append($('events'),recordNode(`${stamp} · 术语准备 · ${d.status === 'started' ? '进行中' : d.status === 'ready' ? '完成' : '使用空术语表'}`,JSON.stringify(d,null,2),true),true); break;
    case 'review_started':
      reviewBusy = true; break;
    case 'review_completed':
      reviewBusy = false;
      for (const s of d.segments) {
        if (s.review_status === 'bypassed') continue;
        const corrected = s.corrected_text === null ? '未生成，使用原文' : s.corrected_text;
        const row = recordNode(`${s.id} · ${reviewLabels[s.review_status]} · ${s.review_seconds}s`, `原始转写：\n${s.raw_text}\n\n校正文本：\n${corrected}`);
        row.classList.add('segment'); append($('transcripts'),row);
        $('reviewTime').textContent = s.review_seconds + 's';
      }
      $('sessionStatus').textContent = '转写已就绪 · 等待学生处理';
      append($('events'),recordNode(`${stamp} · 审核完成 · ${d.sources.join('、')}`,JSON.stringify(d,null,2),true),true); break;
    case 'warning':
      $('reviewNotice').textContent = d.message; $('reviewNotice').classList.remove('hidden');
      append($('events'),recordNode(`${stamp} · 提示`,d.message),true); break;
    case 'llm_request':
      if (d.phase === 'student') { studentBusy = true; studentFailed = false; }
      if (d.phase === 'preparation') $('sessionStatus').textContent = '正在分析课堂范围';
      append($('modelInputs'),recordNode(`${stamp} · ${phaseLabels[d.phase] || d.phase} · 第 ${d.attempt} 次`,JSON.stringify(d.body,null,2),true),true); break;
    case 'llm_output':
      $('llmTime').textContent = d.seconds + 's';
      append($('events'),recordNode(`${stamp} · 模型原始输出 · ${d.phase}`,d.raw,true),true); break;
    case 'state':
      studentBusy = studentFailed = false;
      showState(d.state); $('sessionStatus').textContent = $('mute').checked ? '静音中 · 继续学习' : '继续听课';
      $('studentMood').textContent = d.note || '正在等待你继续讲。';
      append($('events'),recordNode(`${stamp} · 认知更新与候选回复`,JSON.stringify(d,null,2),true),true); break;
    case 'reply': bubble(d.text); showState(d.state); $('studentMood').textContent = '轮到你继续讲了。'; break;
    case 'suppressed': append($('events'),recordNode(`${stamp} · 发言控制`,d.reason),true); break;
    case 'mute': $('mute').checked = d.muted; $('sessionStatus').textContent = d.muted ? '静音中 · 继续学习' : '继续听课'; break;
    case 'speech': $('avatar').classList.toggle('speaking',d.speaking); if(d.speaking) $('studentMood').textContent = '学生正在回应……'; break;
    case 'speech_stopped': $('avatar').classList.remove('speaking'); break;
    case 'error':
      if (d.code === 'model_failure') { studentBusy = false; studentFailed = true; }
      if (starting) glossaryBusy = false;
      showError(d.message,!!d.retryable); append($('events'),recordNode(`${stamp} · 错误`,d.message),true);
      if (starting) { starting = false; syncControls(); $('sessionStatus').textContent = '创建失败，可重试'; }
      if (window.lessonUpload?.busy && (['audio_backlog','audio_failure','asr_failure'].includes(d.code))) window.lessonUpload.stop();
      else if (window.lessonUpload?.phase === 'starting' && window.lessonUpload.busy) window.lessonUpload.abort(d.message);
      else if (d.code === 'audio_backlog' || d.code === 'audio_failure') stopMic();
      if(d.retryable) $('sessionStatus').textContent = '处理暂停 · 可以重试'; break;
    case 'finished':
      glossaryBusy = reviewBusy = studentBusy = studentFailed = false;
      running = false; ending = false; showState(d.state); syncControls(); $('sessionStatus').textContent = '本次试讲已结束';
      $('studentMood').textContent = '这节课结束了。我的当前理解保留在下方。';
      append($('events'),recordNode('结束记录',JSON.stringify(d,null,2),true),true);
      if(d.unprocessed_sources.length) showError('部分课堂内容尚未处理：' + d.unprocessed_sources.join('、') + '。原文已保存在本次记录中。');
      break;
  }
  showActivity();
}
async function connect() {
  if (socket) { socket.onclose = null; socket.close(); }
  socket = new WebSocket(`ws://${location.host}/ws/session`);
  socket.onmessage = e => { try { handle(JSON.parse(e.data)); } catch { showError('接收到无法显示的服务消息'); } };
  socket.onclose = () => {
    window.lessonUpload?.abort('本机服务连接已断开；已有课堂记录保留，未发送部分未进入课堂。');
    if(running || starting) showError('本机服务连接已断开；请检查服务并重新创建试讲。');
    running = false; starting = false; ending = false;
    glossaryBusy = reviewBusy = studentBusy = studentFailed = false; stopMic(false); syncControls();
    window.assessment?.disconnected();
  };
  await new Promise((resolve,reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error('无法连接本机服务')); });
}
$('lessonForm').addEventListener('submit',async e => {
  e.preventDefault(); if(starting || running || !asrReady || asrBusy || asrDownloading || window.asrDebug?.busy) return;
  const topic = $('topic').value.trim(); if(!topic) return;
  starting = true; ending = false; records = []; transcriptCount = 0; segmentCount = 0; sessionId = '';
  window.assessment?.reset();
  glossaryBusy = reviewBusy = studentBusy = studentFailed = false;
  $('transcriptCount').textContent = '0'; $('segmentCount').textContent = '0'; $('llmTime').textContent = '—'; $('reviewTime').textContent = '—';
  for(const id of ['transcripts','events','modelInputs','conversation']) $(id).textContent = '';
  $('error').classList.add('hidden'); $('scope').classList.add('hidden'); $('reviewNotice').classList.add('hidden'); syncControls();
  try { await connect(); send({type:'start',lesson:{topic,points:$('points').value,prerequisites:$('prerequisites').value,level:$('level').value},muted:$('mute').checked,tts:$('tts').checked,asr_review:$('asrReview').checked}); }
  catch(e) { starting = false; syncControls(); showError(e.message); }
});
async function startMic() {
  if (!running || ending || recording || micStarting || micStopping || window.lessonUpload?.busy || window.assessment?.busy) return;
  const generation = ++micGeneration;
  micStarting = true; syncControls();
  $('mic').disabled = true;
  try {
    context = new AudioContext(); await context.resume();
    if (generation !== micGeneration || !running || ending) return;
    const captured = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
    if (generation !== micGeneration || !running || ending) { captured.getTracks().forEach(track => track.stop()); return; }
    stream = captured;
    await context.audioWorklet.addModule('/static/audio-worklet.js');
    if (generation !== micGeneration || !running || ending) return;
    source = context.createMediaStreamSource(stream);
    worklet = new AudioWorkletNode(context,'capture-processor');
    silentGain = context.createGain(); silentGain.gain.value = 0;
    const sendAudioFrame = samples => {
      if(socket?.readyState === WebSocket.OPEN && recording) {
        if(socket.bufferedAmount > 1024 * 1024) { showError('录音发送积压，已暂停麦克风，请稍后重试。'); stopMic(); return; }
        const copy = new Float32Array(samples);
        socket.send(copy.buffer);
        sentAudioFrames += 1;
        $('meterFill').style.width = Math.min(100, rms(copy) * 600) + '%';
      }
    };
    worklet.port.onmessage = e => {
      if(e.data.flushed) { flushed?.(); return; }
      sendAudioFrame(e.data.samples);
    };
    send({type:'audio_start',sample_rate:context.sampleRate});
    recording = true; source.connect(worklet); worklet.connect(silentGain); silentGain.connect(context.destination);
    sentAudioFrames = 0;
    // Some Chrome/macOS input devices load an AudioWorklet but do not pull
    // its render graph. Fall back to the broadly supported ScriptProcessor.
    fallbackTimer = setTimeout(() => {
      if (!recording || sentAudioFrames > 0) return;
      worklet?.disconnect();
      processor = context.createScriptProcessor(2048, 1, 1);
      processor.onaudioprocess = event => sendAudioFrame(event.inputBuffer.getChannelData(0));
      source.connect(processor); processor.connect(silentGain);
      $('micHint').textContent = '已切换兼容录音通道。请继续讲授，停顿后会出现转写。';
    }, 1200);
    $('micHint').textContent = '麦克风已开启。停顿后出现最终转写；学生在静音时也会继续听课。建议戴耳机。';
  } catch(e) {
    if (generation !== micGeneration) return;
    showError(e.name === 'NotAllowedError' ? '麦克风权限未开放，请在浏览器地址栏允许麦克风后重试。' : '麦克风无法启动：' + e.message);
    await stopMic(false);
  } finally {
    if (generation === micGeneration) micStarting = false;
    syncControls();
  }
}
function rms(samples) {
  let sum = 0; for (let i = 0; i < samples.length; i++) sum += samples[i] * samples[i];
  return Math.sqrt(sum / Math.max(1, samples.length));
}
async function stopMic(notify = true) {
  if (notify && running && (recording || micStarting)) micStopping = true;
  micGeneration += 1; micStarting = false;
  if(worklet && recording) {
    await Promise.race([new Promise(resolve => { flushed = resolve; worklet.port.postMessage('flush'); }),new Promise(resolve => setTimeout(resolve,200))]);
  }
  recording = false; flushed = null;
  clearTimeout(fallbackTimer); fallbackTimer = null;
  source?.disconnect(); worklet?.disconnect(); processor?.disconnect(); silentGain?.disconnect();
  stream?.getTracks().forEach(track => track.stop());
  if(context && context.state !== 'closed') await context.close();
  source = worklet = processor = silentGain = context = stream = null;
  if(notify && running && micStopping) send({type:'audio_stop'});
  if (!notify) micStopping = false;
  $('meterFill').style.width = '0%'; syncControls();
}
$('mic').onclick = () => recording ? stopMic() : startMic();
$('stopSpeech').onclick = () => send({type:'stop_speech'});
$('mute').onchange = () => { if(running) send({type:'mute',value:$('mute').checked}); };
$('tts').onchange = () => { if(running) send({type:'tts',value:$('tts').checked}); };
$('textForm').onsubmit = e => { e.preventDefault(); const text = $('teacherText').value.trim(); if(!text || !running || window.lessonUpload?.busy || ending) return; send({type:'text',text}); bubble(text,true); $('teacherText').value = ''; };
$('end').onclick = async () => { if(ending) return; ending = true; syncControls(); await window.lessonUpload?.stop(); await stopMic(); send({type:'end'}); $('sessionStatus').textContent = '正在结束试讲'; };
$('retry').onclick = () => { send({type:'retry'}); $('error').classList.add('hidden'); };
$('dismiss').onclick = () => $('error').classList.add('hidden');
$('export').onclick = () => {
  const blob = new Blob([records.map(e => JSON.stringify(e)).join('\n')],{type:'application/x-ndjson'});
  const url = URL.createObjectURL(blob), a = document.createElement('a'); a.href = url; a.download = `classroom-${sessionId || 'debug'}.jsonl`; a.click(); setTimeout(() => URL.revokeObjectURL(url),1000);
};
for(const tab of document.querySelectorAll('[data-tab]')) tab.onclick = () => {
  for(const t of document.querySelectorAll('[data-tab]')) { const active = t === tab; t.classList.toggle('selected',active); t.setAttribute('aria-selected',String(active)); $('pane-' + t.dataset.tab).classList.toggle('hidden',!active); }
};
window.addEventListener('beforeunload',() => { stream?.getTracks().forEach(t => t.stop()); socket?.close(); });
window.lessonUpload = new AudioFileInput('lessonUpload', {
  readyKind:'audio', finishedText:'音频识别完成；转写已进入课堂，审核和学生处理进度见课堂状态。',
  blocked:() => audioUploadAvailable === null ? '正在等待本机服务连接。' : audioUploadAvailable === false ? '当前后端不支持上传音频，请重启服务并刷新页面。' :
    ending ? '正在结束试讲，请等待处理完成。' : !running || starting ? '请先创建试讲。' : recording || micStarting || micStopping ? '请先暂停麦克风，并等待录音处理结束。' : null,
  changed:() => syncControls(),
  open:metadata => {
    if (socket?.readyState !== WebSocket.OPEN) throw new Error('本机服务未连接');
    send({type:'audio_start', ...metadata});
  },
  send:frame => {
    if (socket?.readyState !== WebSocket.OPEN) throw new Error('本机服务连接已断开');
    socket.send(frame);
  },
  end:cancelled => {
    if (socket?.readyState !== WebSocket.OPEN) throw new Error('本机服务连接已断开');
    send({type:'audio_stop', cancelled});
  },
  disconnected:() => socket?.close()
});
// The model screen shares this page so navigating never disconnects a lesson.
function showPage(models, updateURL = true) {
  $('lessonPage').classList.toggle('hidden', models);
  $('modelsPage').classList.toggle('hidden', !models);
  for (const [id, active] of [['modelsTab', models], ['classroomTab', !models]]) {
    $(id).classList.toggle('selected', active);
    if (active) $(id).setAttribute('aria-current', 'page'); else $(id).removeAttribute('aria-current');
  }
  if (updateURL) history.pushState(null, '', models ? '/models' : '/');
}
$('modelsTab').onclick = () => showPage(true);
$('classroomTab').onclick = () => showPage(false);
$('changeModel').onclick = () => showPage(true);
window.addEventListener('popstate', () => showPage(location.pathname === '/models', false));
showPage(location.pathname === '/models', false);

function formatBytes(size) {
  return size >= 1e9 ? (size / 1e9).toFixed(2) + ' GB' : Math.round(size / 1e6) + ' MB';
}
function modelMessage(message, error = false) {
  $('asrMessage').textContent = message || '';
  $('asrMessage').classList.toggle('hidden', !message);
  $('asrMessage').classList.toggle('failed', error);
}
async function asrRequest(url, method = 'GET') {
  const response = await fetch(url, {method});
  const data = await response.json();
  if (!response.ok) throw new Error(data.detail || '模型操作失败，请重试');
  return data;
}
let actionPending = false, refreshingASR = false, lastModelRender = '', lastNotice = '';
async function modelAction(model, action) {
  if (actionPending) return;
  actionPending = true;
  renderModelCards(lastASRState);
  try {
    await asrRequest(`/api/asr/models/${encodeURIComponent(model.id)}/${action}`, 'POST');
    modelMessage(action === 'select' ? `已选择 ${model.name}，下次试讲使用此模型。` : action === 'cancel' ? '正在取消下载……' : `开始下载 ${model.name}。`);
  } catch (error) { modelMessage(error.message, true); }
  finally { actionPending = false; lastModelRender = ''; await refreshASR(); }
}
let lastASRState;
function renderModelCards(state) {
  if (!state) return;
  const renderKey = JSON.stringify([state.models, state.vad_ready, state.busy, state.activity, running, starting, ending, actionPending, Boolean(window.asrDebug?.busy)]);
  if (renderKey === lastModelRender) return;
  lastModelRender = renderKey;
  const holder = $('modelCards');
  // Keep keyboard focus on the same action across progress updates.
  const focused = document.activeElement?.dataset?.modelAction;
  holder.replaceChildren();
  const busy = state.busy || running || starting || ending || window.asrDebug?.busy;
  const downloading = state.models.some(m => ['downloading', 'verifying', 'cancelling'].includes(m.state));
  const stateNames = {available:'未安装', installed:'已安装', incomplete:'文件不完整', downloading:'下载中', verifying:'校验中', cancelling:'取消中', cancelled:'已取消', failed:'下载失败'};
  for (const model of state.models) {
    const card = document.createElement('article'); card.className = 'card model-card' + (model.selected ? ' selected-model' : '');
    const top = document.createElement('div'); top.className = 'model-card-top';
    const title = document.createElement('h2'); title.textContent = model.name;
    const status = document.createElement('span'); status.className = 'pill'; status.textContent = model.selected ? '当前选择' : stateNames[model.state];
    top.append(title, status);
    const tag = document.createElement('span'); tag.className = 'tag model-label'; tag.textContent = model.label;
    const description = document.createElement('p'); description.className = 'model-description'; description.textContent = model.description;
    const meta = document.createElement('div'); meta.className = 'model-meta';
    const language = document.createElement('span'); language.textContent = model.languages;
    const size = document.createElement('strong'); size.textContent = formatBytes(model.size_bytes);
    meta.append(language, size);
    const links = document.createElement('div'); links.className = 'model-links';
    for (const [text, href] of [['模型来源',model.source], ['权重许可',model.license_url]]) {
      const a = document.createElement('a'); a.textContent = text; a.href = href; a.target = '_blank'; a.rel = 'noopener noreferrer'; links.append(a);
    }
    const directory = document.createElement('code'); directory.textContent = model.directory + '/'; links.append(directory);
    card.append(top, tag, description, meta, links);
    const active = ['downloading', 'verifying', 'cancelling'].includes(model.state);
    if (active) {
      const progress = document.createElement('progress'); progress.max = model.job.total_bytes || 1; progress.value = model.job.downloaded_bytes || 0;
      progress.setAttribute('aria-label', `${model.name} 下载进度`);
      const detail = document.createElement('p'); detail.className = 'hint';
      detail.textContent = `${formatBytes(model.job.downloaded_bytes || 0)} / ${formatBytes(model.job.total_bytes || model.size_bytes)} · ${model.job.file || stateNames[model.state]}`;
      card.append(progress, detail);
    }
    if (model.job.error) { const error = document.createElement('p'); error.className = 'model-error'; error.textContent = model.job.error; card.append(error); }
    const actions = document.createElement('div'); actions.className = 'model-actions';
    function button(text, action, disabled, primary = false) {
      const b = document.createElement('button'); b.textContent = text; b.disabled = disabled || actionPending;
      b.className = primary ? 'primary' : ''; b.dataset.modelAction = model.id + ':' + action;
      b.onclick = () => modelAction(model, action); actions.append(b);
    }
    if (active) button(model.state === 'cancelling' ? '正在取消' : '取消下载', 'cancel', model.state === 'cancelling');
    else if (model.installed) {
      button(model.selected ? '已选择使用' : '选择使用', 'select', busy || model.selected, true);
      if (model.downloadable) button(state.vad_ready ? '校验 / 修复' : '准备 VAD', 'download', busy || downloading);
    } else if (model.downloadable) {
      button(['failed','cancelled','incomplete'].includes(model.state) ? '重新下载' : '下载模型', 'download', busy || downloading, true);
    } else {
      const hint = document.createElement('p'); hint.className = 'hint'; hint.textContent = '开发模型文件不完整，请选择上方标准模型。'; actions.append(hint);
    }
    card.append(actions); holder.append(card);
  }
  if (focused) [...holder.querySelectorAll('button')].find(b => b.dataset.modelAction === focused && !b.disabled)?.focus();
}
async function refreshASR() {
  if (refreshingASR) return;
  refreshingASR = true;
  try {
    const [state, health] = await Promise.all([asrRequest('/api/asr/models'), asrRequest('/api/health')]);
    asrDebugAvailable = health.asr_debug_available === true;
    audioUploadAvailable = health.audio_upload_available === true;
    lastASRState = state; asrReady = state.asr_ready; asrBusy = state.busy;
    asrDownloading = state.models.some(m => ['downloading','verifying','cancelling'].includes(m.state));
    const selected = state.models.find(m => m.selected);
    $('modelsPath').textContent = state.models_dir;
    $('currentASR').textContent = selected ? `当前模型：${selected.name}` : '尚未选择语音模型';
    $('asrSetupHint').textContent = state.busy ? '当前试讲或 ASR 调试已锁定模型，结束后可切换。' : asrDownloading ? '请等待模型下载完成，或取消下载后再创建试讲。' : !state.vad_ready && selected ? '共享 VAD 缺失，请在模型页点击「准备 VAD」。' : state.asr_ready ? '开始试讲或调试时加载；空闲时可直接删除模型文件夹。' : '开始前请下载并选择本机语音识别模型。';
    $('modelsBusy').classList.toggle('hidden', !(state.busy || running || starting || ending || window.asrDebug?.busy));
    $('modelsBusy').textContent = state.activity === 'debug' || window.asrDebug?.busy ? 'ASR 调试正在准备、录音或处理。结束测试后可以下载或切换模型。' : '试讲创建、进行或结束处理中。结束后可以下载或切换模型。';
    $('openModelsFolder').disabled = actionPending;
    $('health').textContent = state.engine_state === 'loading' ? '正在加载本机 ASR' : state.activity === 'debug' ? '本机 ASR 调试中' : state.error ? 'ASR 加载失败 · 请检查模型' : !asrReady ? '请配置 ASR 模型' : !health.model_configured ? '本机 ASR 已配置 · 请配置 LLM 密钥' : '本机 ASR 已配置 · LLM 已配置';
    if (state.notice && state.notice !== lastNotice) { modelMessage(state.notice, true); lastNotice = state.notice; }
    if (state.error) modelMessage(state.error, true);
    renderModelCards(state); syncControls();
  } catch (error) {
    asrReady = false; asrDebugAvailable = null; audioUploadAvailable = null; $('health').textContent = '本机服务未连接'; syncControls();
    modelMessage('无法读取本机模型状态，请检查服务是否启动。', true);
  } finally { refreshingASR = false; }
}
$('refreshModels').onclick = () => refreshASR();
$('openModelsFolder').onclick = async () => {
  try { await asrRequest('/api/asr/open-folder', 'POST'); }
  catch (error) { modelMessage(error.message, true); }
};
refreshASR();
setInterval(refreshASR, 1500);
