'use strict';
// PCM capture for standalone ASR diagnostics, using the classroom worklet.
class PCMRecorder {
  constructor({onFrame, onLevel, onFallback}) {
    this.onFrame = onFrame; this.onLevel = onLevel; this.onFallback = onFallback;
    this.recording = false; this.closed = false; this.frames = 0;
  }
  async prepare() {
    this.context = new AudioContext();
    await this.context.resume();
    this.stream = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
    if (this.closed) { this.stream.getTracks().forEach(t => t.stop()); throw new Error('录音准备已取消'); }
    await this.context.audioWorklet.addModule('/static/audio-worklet.js');
    if (this.closed) throw new Error('录音准备已取消');
    this.source = this.context.createMediaStreamSource(this.stream);
    this.worklet = new AudioWorkletNode(this.context, 'capture-processor');
    this.gain = this.context.createGain(); this.gain.gain.value = 0;
    this.worklet.port.onmessage = event => {
      if (event.data.flushed) { this.flushed?.(); return; }
      this.frame(event.data.samples);
    };
    return this.context.sampleRate;
  }
  frame(samples) {
    if (!this.recording) return;
    const copy = new Float32Array(samples);
    let sum = 0; for (const value of copy) sum += value * value;
    this.onLevel(Math.min(100, Math.sqrt(sum / Math.max(1, copy.length)) * 600));
    this.onFrame(copy);
    this.frames++;
  }
  start() {
    if (this.closed) return;
    this.recording = true;
    this.source.connect(this.worklet); this.worklet.connect(this.gain); this.gain.connect(this.context.destination);
    this.fallback = setTimeout(() => {
      if (!this.recording || this.frames) return;
      this.source.disconnect(this.worklet); this.worklet.disconnect();
      this.processor = this.context.createScriptProcessor(2048, 1, 1);
      this.processor.onaudioprocess = event => this.frame(event.inputBuffer.getChannelData(0));
      this.source.connect(this.processor); this.processor.connect(this.gain);
      this.onFallback();
    }, 1200);
  }
  async close(flush = false) {
    if (flush && this.worklet && this.recording && !this.processor) {
      await Promise.race([new Promise(resolve => { this.flushed = resolve; this.worklet.port.postMessage('flush'); }),
                          new Promise(resolve => setTimeout(resolve, 200))]);
    }
    this.recording = false; this.closed = true; this.flushed = null;
    clearTimeout(this.fallback);
    this.source?.disconnect(); this.worklet?.disconnect(); this.processor?.disconnect(); this.gain?.disconnect();
    this.stream?.getTracks().forEach(t => t.stop());
    if (this.context && this.context.state !== 'closed') await this.context.close();
    this.onLevel(0);
  }
}
