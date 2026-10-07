'use strict';
(() => {
  const state = window.asrDebug = {busy:false, phase:'idle', records:[], model:null, count:0, seconds:0};
  let socket, recorder, stopPromise, generation = 0;
  const labels = {idle:'尚未开始',preparing:'准备麦克风',loading:'加载模型中',recording:'正在录音',finishing:'处理末尾录音',finished:'测试已结束',error:'测试中断'};
  function status(phase, text) {
    state.phase = phase; $('asrDebugStatus').textContent = text || labels[phase]; sync();
  }
  function error(message) {
    $('asrDebugError').textContent = message; $('asrDebugError').classList.remove('hidden');
  }
  function sync() {
    $('asrDebugStart').disabled = state.busy || state.upload?.busy || running || starting || ending || asrBusy || asrDownloading || !asrReady || asrDebugAvailable !== true;
    $('asrDebugStop').disabled = !state.busy || Boolean(state.upload?.busy) || state.phase === 'finishing';
    $('asrDebugStop').textContent = ['preparing','loading'].includes(state.phase) ? '取消准备' : '结束录音';
    $('asrDebugClear').disabled = state.busy || state.upload?.busy;
    $('asrDebugExport').disabled = !state.records.length || state.busy || state.upload?.busy;
    if (!state.busy) $('asrDebugHint').textContent = asrDebugAvailable === false ? '当前后端版本不支持 ASR 调试。请在启动服务的终端按 Control-C，再重新运行 python run.py，并刷新页面。' : asrDebugAvailable === null ? '正在等待本机服务连接；请确认服务已启动。' : running || starting || ending || asrBusy ? '请先结束正在进行的试讲或其他 ASR 测试，再开始录音。' : !asrReady ? '请先安装并选择模型；如果 VAD 缺失，请先准备 VAD。' : asrDownloading ? '请等待模型下载完成或取消下载后再测试。' : '使用当前选择的模型。只做本机语音转文字，不调用 LLM，不播放声音，不保存音频文件。';
    state.upload?.sync();
  }
  state.sync = sync;
  function syncAll() { syncControls(); renderModelCards(lastASRState); sync(); }
  function clear() {
    state.records = []; state.model = null; state.count = state.seconds = 0;
    $('asrDebugCount').textContent = '0'; $('asrDebugDuration').textContent = '0s'; $('asrDebugSeconds').textContent = '0s'; $('asrDebugModel').textContent = '—';
    $('asrDebugResults').replaceChildren();
    const empty = document.createElement('p'); empty.className = 'empty'; empty.textContent = '尚无转写。开始录音后，停顿时会显示最终转写。'; $('asrDebugResults').append(empty);
    $('asrDebugError').classList.add('hidden'); status('idle');
  }
  function cleanup(expected = false) {
    recorder?.close().catch(() => {});
    if (state.upload?.busy) state.upload.abort(expected ? '测试中断，已有转写保留。' : '本机调试连接已断开，已有转写保留。');
    state.busy = false; syncAll(); refreshASR();
    if (!expected && state.phase !== 'error') {
      error('本机调试连接已断开。已有转写保留，请检查服务后重新测试。'); status('error');
    }
  }
  function handle(event, current) {
    if (current !== generation) return;
    const data = event.data || {};
    state.upload?.handle(event);
    switch (event.type) {
      case 'status':
        $('asrDebugStatus').textContent = data.message; break;
      case 'ready':
        state.model = data.model_id;
        const model = lastASRState?.models.find(m => m.id === data.model_id);
        $('asrDebugModel').textContent = model?.name || data.model_id;
        if (data.input_source === 'audio_file') {
          status('recording', '正在处理上传音频');
          $('asrDebugHint').textContent = '使用录音相同的 VAD 分段，快速逐段识别；不播放音频。';
        } else {
          recorder.start(); status('recording');
          $('asrDebugHint').textContent = '麦克风已开启。自然讲授并稍作停顿，观察转写、识别耗时和实时系数。';
        }
        refreshASR(); break;
      case 'vad': $('asrDebugStatus').textContent = data.speaking ? '检测到语音' : '正在识别语音段'; break;
      case 'audio_input': $('asrDebugDuration').textContent = data.audio_seconds.toFixed(1) + 's'; break;
      case 'transcript': {
        state.records.push({...data, time:new Date().toISOString()});
        state.count++; state.seconds += data.asr_seconds;
        $('asrDebugCount').textContent = state.count; $('asrDebugSeconds').textContent = state.seconds.toFixed(2) + 's';
        $('asrDebugResults').querySelector('.empty')?.remove();
        const node = recordNode(`第 ${data.index} 段${data.filename ? ' · ' + data.filename : ''} · 音频 ${data.audio_seconds}s · 识别 ${data.asr_seconds}s · 实时系数 ${data.real_time_factor}`, data.text || '本段未识别出文字');
        $('asrDebugResults').append(node);
        $('asrDebugResults').scrollTop = $('asrDebugResults').scrollHeight;
        break;
      }
      case 'error':
        error(data.message);
        if (data.code === 'audio_backlog') stop();
        else { status('error'); socket?.close(); cleanup(true); }
        break;
      case 'finished':
        $('asrDebugDuration').textContent = data.audio_seconds.toFixed(1) + 's';
        $('asrDebugSeconds').textContent = data.asr_seconds.toFixed(2) + 's';
        status('finished', data.cancelled ? '已停止，仅处理已接收部分' : '测试已结束'); cleanup(true); socket?.close(); break;
    }
  }
  async function start(metadata = null) {
    if (state.busy || asrBusy || running || starting || ending || !asrReady || asrDownloading || asrDebugAvailable !== true) return;
    clear(); state.busy = true; status('preparing', metadata ? '准备上传音频' : null); syncAll();
    const current = ++generation; stopPromise = null;
    recorder = metadata ? null : new PCMRecorder({
      onFrame: samples => {
        if (current !== generation || socket?.readyState !== WebSocket.OPEN) return;
        if (socket.bufferedAmount > 1024 * 1024) { error('音频发送积压，正在结束录音；已接收内容继续处理。'); stop(); return; }
        socket.send(samples.buffer);
      },
      onLevel: level => { if (current === generation) $('asrDebugMeter').style.width = level + '%'; },
      onFallback: () => { $('asrDebugHint').textContent = '已启用兼容录音通道，请继续讲授并停顿查看转写。'; }
    });
    const capture = recorder;
    try {
      const sampleRate = metadata ? metadata.sample_rate : await capture.prepare();
      if (current !== generation || !state.busy) { await capture?.close(); return; }
      socket = new WebSocket(`ws://${location.host}/ws/asr-debug`);
      socket.onmessage = e => { try { handle(JSON.parse(e.data), current); } catch { error('无法显示调试结果，请重新测试。'); } };
      socket.onclose = () => { if (current === generation && state.busy) cleanup(false); };
      await new Promise((resolve, reject) => { socket.onopen = resolve; socket.onerror = () => reject(new Error('无法连接本机 ASR 调试服务，请确认后端已重启到最新版本并刷新页面')); });
      if (current !== generation || !state.busy) { socket.close(); return; }
      status('loading'); socket.send(JSON.stringify({type:'start', sample_rate:sampleRate, ...(metadata || {})}));
    } catch (exc) {
      await capture?.close().catch(() => {});
      if (current !== generation) return;
      error(exc.name === 'NotAllowedError' ? '请允许浏览器使用麦克风，再开始测试。' : '录音测试无法启动：' + exc.message);
      status('error'); socket?.close(); cleanup(true);
    }
  }
  async function stop() {
    if (!state.busy || stopPromise) return;
    if (['preparing','loading'].includes(state.phase)) {
      ++generation; socket?.close(); await recorder?.close().catch(() => {});
      status('finished', '已取消准备'); cleanup(true); return;
    }
    status('finishing');
    stopPromise = (async () => {
      await recorder?.close(true);
      if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({type:'stop'}));
      else cleanup(false);
    })();
    try { await stopPromise; } catch (exc) { error('结束录音失败：' + exc.message); socket?.close(); cleanup(true); }
  }
  state.upload = new AudioFileInput('debugUpload', {
    readyKind:'ready', finishedText:'音频识别完成，可导出结果或切换模型再次测试。',
    blocked:() => audioUploadAvailable === null ? '正在等待本机服务连接。' : audioUploadAvailable === false ? '当前后端不支持上传音频，请重启服务并刷新页面。' :
      state.busy || running || starting || ending || asrBusy ? '请先结束试讲或正在进行的 ASR 测试。' :
      !asrReady ? '请先安装并选择模型，准备 VAD。' : asrDownloading ? '请先等待模型下载完成。' : null,
    changed:() => syncAll(), open:metadata => start(metadata),
    send:frame => {
      if (socket?.readyState !== WebSocket.OPEN) throw new Error('调试连接已断开');
      socket.send(frame);
    },
    end:cancelled => {
      if (socket?.readyState !== WebSocket.OPEN) throw new Error('调试连接已断开');
      socket.send(JSON.stringify({type:'stop', cancelled}));
    },
    disconnected:() => socket?.close()
  });
  $('asrDebugStart').onclick = () => start(); $('asrDebugStop').onclick = stop;
  $('asrDebugClear').onclick = clear;
  $('asrDebugExport').onclick = () => {
    const report = {project:'讲给我听 / Learn From You',model_id:state.model,transcripts:state.records};
    const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type:'application/json'}));
    const anchor = document.createElement('a'); anchor.href = url; anchor.download = `asr-debug-${state.model || 'result'}-${Date.now()}.json`; anchor.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  window.addEventListener('beforeunload', () => { recorder?.close().catch(() => {}); socket?.close(); });
  sync();
})();
