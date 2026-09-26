'use strict';
const $ = id => document.getElementById(id);
let socket, running = false, starting = false, recording = false, ending = false;
let context, stream, source, worklet, silentGain, flushed;
let records = [], transcriptCount = 0, segmentCount = 0, sessionId = '';
const labels = {tentative:'暂定理解',understood:'当前理解',unclear:'信息缺失',conflict:'存在冲突',pending:'待提问',asked:'已提问',resolved:'已解决',deferred:'暂缓'};

function send(data) { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data)); }
function showError(message, retry = false) {
  $('errorText').textContent = message; $('error').classList.remove('hidden'); $('retry').classList.toggle('hidden', !retry);
}
function syncControls() {
  $('start').disabled = running || starting;
  for (const id of ['mic','stopSpeech','end','teacherText','sendText']) $(id).disabled = !running || ending;
  for (const id of ['topic','points','prerequisites','level']) $(id).disabled = running || starting;
  $('mic').textContent = recording ? '暂停麦克风' : '开启麦克风';
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
  for (const [name, items] of [['当前认知',state.knowledge],['问题与解答',state.questions]]) {
    const heading = document.createElement('div'); heading.className = 'state-title'; heading.textContent = name; holder.append(heading);
    if (!items.length) { const p = document.createElement('p'); p.className = 'hint'; p.textContent = '暂无记录'; holder.append(p); }
    for (const item of items) {
      const row = document.createElement('div'); row.className = 'state-item ' + item.status;
      const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = labels[item.status];
      const text = document.createElement('span'); text.textContent = item.text;
      const meta = document.createElement('small'); meta.textContent = `${item.id} · 来源 ${item.sources.join('、')}${item.attempts !== undefined ? ' · 已提问 ' + item.attempts + ' 次' : ''}${item.resolution_sources?.length ? ' · 解答来源 ' + item.resolution_sources.join('、') : ''}`;
      row.append(tag,text,meta); holder.append(row);
    }
  }
}
function handle(event) {
  records.push(event); const d = event.data || {}, stamp = event.elapsed === undefined ? '' : `${event.elapsed.toFixed(1)}s`;
  switch (event.type) {
    case 'ready':
      starting = false; running = true; sessionId = d.session_id; $('sessionStatus').textContent = '课堂已就绪'; $('studentMood').textContent = '这节课的内容，我准备开始听了。';
      $('scope').classList.remove('hidden'); $('scopeTags').textContent = '';
      for (const name of d.scope.scope) { const tag = document.createElement('span'); tag.className = 'tag'; tag.textContent = name; $('scopeTags').append(tag); }
      $('boundary').textContent = d.scope.boundary_note; showState(d.state); syncControls(); break;
    case 'status': $('sessionStatus').textContent = d.message; break;
    case 'audio': $('sessionStatus').textContent = d.recording ? '正在听课' : '麦克风已暂停'; break;
    case 'vad': $('avatar').classList.toggle('listening',d.speaking); $('studentMood').textContent = d.speaking ? '正在听你讲……' : '正在整理刚才听到的内容。'; break;
    case 'transcript':
      $('transcriptCount').textContent = ++transcriptCount;
      append($('transcripts'),recordNode(`${d.id} · ${d.mode === 'microphone' ? '本机 ASR' : '文字测试'} · ${stamp}${d.asr_seconds !== undefined ? ' · 识别 ' + d.asr_seconds + 's' : ''}`,d.text));
      $('transcripts').parentElement.scrollTop = $('transcripts').parentElement.scrollHeight; break;
    case 'segment': {
      $('segmentCount').textContent = ++segmentCount;
      const row = recordNode(`教学片段 · ${d.sources.join('、')} · ${d.reason}`,d.text); row.classList.add('segment'); append($('transcripts'),row); break;
    }
    case 'llm_request':
      $('sessionStatus').textContent = d.phase === 'preparation' ? '正在分析课堂范围' : '学生正在消化';
      append($('modelInputs'),recordNode(`${stamp} · ${d.phase === 'preparation' ? '课前范围分析' : '学生认知更新'} · 第 ${d.attempt} 次`,JSON.stringify(d.body,null,2),true),true); break;
    case 'llm_output':
      $('llmTime').textContent = d.seconds + 's';
      append($('events'),recordNode(`${stamp} · 模型原始输出 · ${d.phase}`,d.raw,true),true); break;
    case 'state':
      showState(d.state); $('sessionStatus').textContent = $('mute').checked ? '静音中 · 继续学习' : '继续听课';
      $('studentMood').textContent = d.note || '正在等待你继续讲。';
      append($('events'),recordNode(`${stamp} · 认知更新与候选回复`,JSON.stringify(d,null,2),true),true); break;
    case 'reply': bubble(d.text); showState(d.state); $('studentMood').textContent = '轮到你继续讲了。'; break;
    case 'suppressed': append($('events'),recordNode(`${stamp} · 发言控制`,d.reason),true); break;
    case 'mute': $('mute').checked = d.muted; $('sessionStatus').textContent = d.muted ? '静音中 · 继续学习' : '继续听课'; break;
    case 'speech': $('avatar').classList.toggle('speaking',d.speaking); if(d.speaking) $('studentMood').textContent = '学生正在回应……'; break;
    case 'speech_stopped': $('avatar').classList.remove('speaking'); break;
    case 'error':
      showError(d.message,!!d.retryable); append($('events'),recordNode(`${stamp} · 错误`,d.message),true);
      if (starting) { starting = false; syncControls(); $('sessionStatus').textContent = '创建失败，可重试'; }
      if (d.code === 'audio_backlog' || d.code === 'audio_failure') stopMic();
      if(d.retryable) $('sessionStatus').textContent = '处理暂停 · 可以重试'; break;
    case 'finished':
      running = false; ending = false; showState(d.state); syncControls(); $('sessionStatus').textContent = '本次试讲已结束';
      $('studentMood').textContent = '这节课结束了。我的当前理解保留在下方。';
      append($('events'),recordNode('结束记录',JSON.stringify(d,null,2),true),true);
      if(d.unprocessed_sources.length) showError('部分课堂内容尚未处理：' + d.unprocessed_sources.join('、') + '。原文已保存在本次记录中。');
      break;
  }
}
async function connect() {
  if (socket) { socket.onclose = null; socket.close(); }
  socket = new WebSocket(`ws://${location.host}/ws/session`);
  socket.onmessage = e => { try { handle(JSON.parse(e.data)); } catch { showError('接收到无法显示的服务消息'); } };
  socket.onclose = () => {
    if(running || starting) showError('本机服务连接已断开；请检查服务并重新创建试讲。');
    running = false; starting = false; ending = false; stopMic(false); syncControls();
  };
  await new Promise((resolve,reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error('无法连接本机服务')); });
}
$('lessonForm').addEventListener('submit',async e => {
  e.preventDefault(); if(starting || running) return;
  const topic = $('topic').value.trim(); if(!topic) return;
  starting = true; ending = false; records = []; transcriptCount = 0; segmentCount = 0; sessionId = '';
  $('transcriptCount').textContent = '0'; $('segmentCount').textContent = '0'; $('llmTime').textContent = '—';
  for(const id of ['transcripts','events','modelInputs','conversation']) $(id).textContent = '';
  $('error').classList.add('hidden'); $('scope').classList.add('hidden'); syncControls();
  try { await connect(); send({type:'start',lesson:{topic,points:$('points').value,prerequisites:$('prerequisites').value,level:$('level').value},muted:$('mute').checked,tts:$('tts').checked}); }
  catch(e) { starting = false; syncControls(); showError(e.message); }
});
async function startMic() {
  $('mic').disabled = true;
  try {
    context = new AudioContext(); await context.resume();
    stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
    await context.audioWorklet.addModule('/static/audio-worklet.js');
    source = context.createMediaStreamSource(stream);
    worklet = new AudioWorkletNode(context,'capture-processor');
    silentGain = context.createGain(); silentGain.gain.value = 0;
    worklet.port.onmessage = e => {
      if(e.data.flushed) { flushed?.(); return; }
      if(socket?.readyState === WebSocket.OPEN && recording) {
        if(socket.bufferedAmount > 1024 * 1024) { showError('录音发送积压，已暂停麦克风，请稍后重试。'); stopMic(); return; }
        socket.send(e.data.samples.buffer); $('meterFill').style.width = Math.min(100,e.data.rms * 600) + '%';
      }
    };
    send({type:'audio_start',sample_rate:context.sampleRate});
    recording = true; source.connect(worklet); worklet.connect(silentGain); silentGain.connect(context.destination);
    $('micHint').textContent = '麦克风已开启。停顿后出现最终转写；学生在静音时也会继续听课。建议戴耳机。';
  } catch(e) {
    showError(e.name === 'NotAllowedError' ? '麦克风权限未开放，请在浏览器地址栏允许麦克风后重试。' : '麦克风无法启动：' + e.message);
    await stopMic(false);
  }
  syncControls();
}
async function stopMic(notify = true) {
  if(worklet && recording) {
    await Promise.race([new Promise(resolve => { flushed = resolve; worklet.port.postMessage('flush'); }),new Promise(resolve => setTimeout(resolve,200))]);
  }
  recording = false; flushed = null;
  source?.disconnect(); worklet?.disconnect(); silentGain?.disconnect();
  stream?.getTracks().forEach(track => track.stop());
  if(context && context.state !== 'closed') await context.close();
  source = worklet = silentGain = context = stream = null;
  if(notify && running) send({type:'audio_stop'});
  $('meterFill').style.width = '0%'; syncControls();
}
$('mic').onclick = () => recording ? stopMic() : startMic();
$('stopSpeech').onclick = () => send({type:'stop_speech'});
$('mute').onchange = () => { if(running) send({type:'mute',value:$('mute').checked}); };
$('tts').onchange = () => { if(running) send({type:'tts',value:$('tts').checked}); };
$('textForm').onsubmit = e => { e.preventDefault(); const text = $('teacherText').value.trim(); if(!text || !running) return; send({type:'text',text}); bubble(text,true); $('teacherText').value = ''; };
$('end').onclick = async () => { if(ending) return; ending = true; syncControls(); await stopMic(); send({type:'end'}); $('sessionStatus').textContent = '正在结束试讲'; };
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
fetch('/api/health').then(r => r.json()).then(h => {
  $('health').textContent = h.asr_ready && h.model_configured ? '本机 ASR 已就绪 · DeepSeek 已配置' : '服务需要检查';
  if(h.error) showError(h.error); else if(!h.model_configured) showError('请在项目 .env 中配置 Deepseek_API。');
}).catch(() => { $('health').textContent = '本机服务未连接'; });
