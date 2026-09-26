class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super(); this.buffer = new Float32Array(2048); this.offset = 0; this.stopped = false;
    this.port.onmessage = e => {
      if (e.data === 'flush') { this.send(); this.stopped = true; this.port.postMessage({flushed: true}); }
    };
  }
  send() {
    if (!this.offset) return;
    const samples = this.buffer.slice(0, this.offset);
    let sum = 0; for (const value of samples) sum += value * value;
    this.port.postMessage({samples, rms: Math.sqrt(sum / samples.length)}, [samples.buffer]);
    this.offset = 0;
  }
  process(inputs) {
    if (this.stopped) return true;
    const channels = inputs[0];
    if (!channels || !channels.length) return true;
    for (let i = 0; i < channels[0].length; i++) {
      let sample = 0; for (const channel of channels) sample += channel[i];
      this.buffer[this.offset++] = sample / channels.length;
      if (this.offset === this.buffer.length) this.send();
    }
    return true;
  }
}
registerProcessor('capture-processor', CaptureProcessor);
