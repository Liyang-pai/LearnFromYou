// 正式前端录音资源：独立持有音轨与音频图，页面切换不影响采集。
export async function requestMicrophone(getMedia, timeout = 30000) {
  let expired=false,timer;
  const pending=getMedia();
  pending.then(stream=>{if(expired)stream.getTracks().forEach(track=>track.stop());},()=>{});
  try{return await Promise.race([pending,new Promise((_,reject)=>{timer=setTimeout(()=>{expired=true;reject(new Error('等待麦克风权限超时，请在浏览器允许麦克风后重试'));},timeout);})]);}
  finally{clearTimeout(timer);}
}
export class Microphone {
  constructor() { this.active = false; this.busy = false; this.generation = 0; }
  async start(socket, startEvent = rate => ({type:'audio_start', sample_rate:rate}), onFailure = () => {}) {
    // 旧音频图关闭完成后才允许建立新图，避免旧清理清空新音轨的引用。
    if (this.stopping) await this.stopping;
    if (this.active || this.busy) return;
    this.busy = true;
    const generation = ++this.generation;
    const ensureCurrent = () => { if (generation !== this.generation) throw new Error('录音准备已取消'); };
    try {
      this.context = new AudioContext();
      await this.context.resume();
      ensureCurrent();
      const stream = await requestMicrophone(()=>navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1,...(this.deviceId?{deviceId:{exact:this.deviceId}}:{})}}));
      if (generation !== this.generation) {
        stream.getTracks().forEach(track => track.stop());
        ensureCurrent();
      }
      this.stream = stream;
      if (socket.readyState !== WebSocket.OPEN) throw new Error('本机连接已断开');
      this.source = this.context.createMediaStreamSource(this.stream);
      this.gain = this.context.createGain(); this.gain.gain.value = 0;
      const frame = samples => {
        if (!this.active || socket.readyState !== WebSocket.OPEN) return;
        if (socket.bufferedAmount > 1024 * 1024) { void this.stop(socket); onFailure(new Error('录音发送积压，麦克风已暂停，请稍后重试')); return; }
        socket.send(new Float32Array(samples).buffer); this.frames++;
      };
      try {
        await this.context.audioWorklet.addModule('/static/formal/capture.js');
        ensureCurrent();
        this.worklet = new AudioWorkletNode(this.context,'capture-processor');
        this.worklet.port.onmessage = e => e.data.flushed ? this.flushed?.() : frame(e.data.samples);
        this.source.connect(this.worklet); this.worklet.connect(this.gain);
      } catch { ensureCurrent(); this.fallback(frame); }
      ensureCurrent();
      this.gain.connect(this.context.destination);
      socket.send(JSON.stringify(startEvent(this.context.sampleRate)));
      this.active = true; this.frames = 0;
      this.fallbackTimer = setTimeout(() => {
        if (this.active && !this.frames && !this.processor) {
          this.worklet?.disconnect(); this.source.disconnect(); this.fallback(frame);
        }
      }, 1800);
    } catch (error) { await this.dispose(); throw error; }
    finally { this.busy = false; }
  }
  fallback(frame) {
    this.processor = this.context.createScriptProcessor(2048,1,1);
    this.processor.onaudioprocess = event => frame(event.inputBuffer.getChannelData(0));
    this.source.connect(this.processor); this.processor.connect(this.gain);
  }
  async stop(socket, stopEvent = {type:'audio_stop'}) {
    if (this.stopping) return this.stopping;
    // 结束、断线或取消准备后，迟到的权限结果不能再启动采集。
    this.generation++;
    this.stopping = (async () => {
      clearTimeout(this.fallbackTimer);
      if (this.worklet && this.active) await new Promise(resolve => {
        this.flushed = resolve; this.worklet.port.postMessage('flush'); setTimeout(resolve,300);
      });
      this.active = false;
      if (socket?.readyState === WebSocket.OPEN && stopEvent) socket.send(JSON.stringify(stopEvent));
      await this.dispose();
    })();
    try { await this.stopping; } finally { this.stopping = null; }
  }
  async dispose() {
    this.active = false; clearTimeout(this.fallbackTimer);
    const context = this.context, stream = this.stream;
    const nodes = [this.source,this.worklet,this.processor,this.gain];
    // 先移交本次资源引用，重复清理不会再次关闭同一音频图。
    this.context = this.stream = this.source = this.worklet = this.processor = this.gain = null;
    stream?.getTracks().forEach(track => track.stop());
    for (const node of nodes) { try { node?.disconnect(); } catch {} }
    if (context && context.state !== 'closed') await context.close();
  }
}
