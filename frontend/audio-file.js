// 两页共用的音频文件输入：浏览器解码、单声道转换、逐帧确认发送和停止处理。
'use strict';
class AudioFileInput {
  constructor(prefix, options) {
    this.options = options;
    this.file = null; this.audio = null; this.busy = false; this.phase = 'idle';
    this.generation = 0; this.serverStarted = false; this.cancelled = false;
    this.elements = Object.fromEntries(['File','Start','Stop','Status','Progress'].map(name => [name, document.getElementById(prefix + name)]));
    this.elements.File.onchange = () => this.select(this.elements.File.files[0]);
    this.elements.Start.onclick = () => this.start();
    this.elements.Stop.onclick = () => this.stop();
    this.sync();
  }
  sync() {
    const reason = this.options.blocked();
    this.elements.File.disabled = this.busy;
    this.elements.Start.disabled = this.busy || !this.audio || Boolean(reason);
    this.elements.Stop.disabled = !this.busy || this.phase === 'finishing';
    this.elements.Start.title = reason || '';
    if (!this.busy) this.elements.Status.textContent = [this.message || '选择 WAV、MP3 或 M4A，最大 50 MiB、最长 10 分钟。', reason].filter(Boolean).join(' ');
  }
  status(phase, message) {
    this.phase = phase; this.message = message; this.elements.Status.textContent = message;
    this.sync(); this.options.changed?.();
  }
  async select(file) {
    if (this.busy) return;
    this.file = file || null; this.audio = null;
    this.elements.Progress.value = 0;
    if (!file) { this.status('idle', '请选择音频文件。'); return; }
    if (!file.size || file.size > 50 * 1024 * 1024) {
      this.status('error', '音频不能为空，且文件大小不得超过 50 MiB。'); return;
    }
    const current = ++this.generation;
    this.busy = true; this.status('decoding', `正在解析 ${file.name}……`);
    let context;
    try {
      context = new AudioContext();
      const decoded = await context.decodeAudioData(await file.arrayBuffer());
      if (current !== this.generation) return;
      if (!decoded.length || !decoded.numberOfChannels || !Number.isFinite(decoded.duration) || decoded.duration > 600) throw new Error('音频不能为空，且时长不得超过 10 分钟。');
      if (decoded.sampleRate < 8000 || decoded.sampleRate > 96000) throw new Error('不支持此音频采样率，请转换为 PCM WAV 或 MP3。');
      const samples = new Float32Array(decoded.length);
      for (let channel = 0; channel < decoded.numberOfChannels; channel++) {
        const data = decoded.getChannelData(channel);
        for (let i = 0; i < samples.length; i++) samples[i] += data[i] / decoded.numberOfChannels;
      }
      if (!samples.every(Number.isFinite)) throw new Error('音频包含无效采样数据。');
      this.audio = {samples, sampleRate:decoded.sampleRate, duration:decoded.duration};
      this.busy = false; this.status('ready', `${file.name} · ${decoded.duration.toFixed(1)} 秒 · 已就绪`);
    } catch (exc) {
      if (current !== this.generation) return;
      this.busy = false;
      this.status('error', exc.name === 'EncodingError' || exc.name === 'NotSupportedError' ? '浏览器无法解码此文件，请转换为 PCM WAV 或 MP3。' : `音频解析失败：${exc.message}`);
    } finally { if (context && context.state !== 'closed') await context.close().catch(() => {}); }
  }
  async start() {
    if (this.busy || !this.audio || this.options.blocked()) return;
    this.busy = true; this.cancelled = false; this.serverStarted = false; this.endSent = false;
    this.id = globalThis.crypto?.randomUUID?.() || `upload-${Date.now()}-${Math.random().toString(16).slice(2)}`;
    this.elements.Progress.value = 0;
    this.done = new Promise(resolve => { this.resolveDone = resolve; });
    this.status('starting', '正在准备音频测试……');
    this.armTimeout();
    try {
      await this.options.open({input_source:'audio_file', upload_id:this.id, filename:this.file.name,
        total_samples:this.audio.samples.length, sample_rate:this.audio.sampleRate});
    } catch (exc) { this.abort(`上传无法启动：${exc.message}`); }
  }
  armTimeout() {
    clearTimeout(this.timer);
    this.timer = setTimeout(() => {
      this.options.disconnected?.();
      this.abort('音频处理长时间没有响应。已有结果保留，请重新连接后测试。');
    }, 180000);
  }
  handle(event) {
    const data = event.data || {};
    if (!this.busy || data.upload_id !== this.id) return;
    if (event.type === this.options.readyKind) {
      this.serverStarted = true; clearTimeout(this.timer);
      if (this.cancelled) this.sendEnd(); else this.pump();
    } else if (event.type === 'upload_ack') {
      if (data.processed_samples !== this.expectedSamples) return;
      this.elements.Progress.value = 100 * data.processed_samples / this.audio.samples.length;
      if (this.phase !== 'finishing') this.status(this.phase, `已处理 ${data.audio_seconds.toFixed(1)} / ${this.audio.duration.toFixed(1)} 秒 · ${Math.round(this.elements.Progress.value)}%`);
      clearTimeout(this.timer); this.resolveFrame?.(true); this.resolveFrame = null;
    } else if (event.type === 'upload_finished') {
      this.elements.Progress.value = 100 * data.processed_samples / this.audio.samples.length;
      this.complete(data.failed ? '处理失败，已有结果保留。' : data.cancelled ? '已停止，仅处理已接收部分。' : this.options.finishedText);
    }
  }
  async pump() {
    this.status('sending', '正在快速处理音频……');
    const {samples} = this.audio;
    try {
      for (let offset = 0; offset < samples.length && this.busy && !this.cancelled; offset += 2048) {
        const frame = samples.slice(offset, offset + 2048);
        this.expectedSamples = offset + frame.length;
        const ack = new Promise(resolve => { this.resolveFrame = resolve; });
        this.armTimeout();
        this.options.send(frame.buffer);
        if (!await ack) return;
      }
      if (this.busy) this.sendEnd();
    } catch (exc) {
      this.options.disconnected?.(); this.abort(`发送中断，已有结果保留：${exc.message}`);
    }
  }
  async stop() {
    if (!this.busy) return;
    if (this.phase === 'decoding') {
      ++this.generation; this.audio = null; this.busy = false;
      this.elements.File.value = '';
      this.status('idle', '已取消解析，请重新选择音频。'); return;
    }
    this.cancelled = true;
    this.resolveFrame?.(false); this.resolveFrame = null;
    if (this.serverStarted) this.sendEnd();
    else this.status('starting', '正在取消准备，等待后端确认……');
    return this.done;
  }
  sendEnd() {
    if (this.endSent || !this.busy) return;
    this.endSent = true;
    this.status('finishing', this.cancelled ? '已停止发送，正在处理已接收音频……' : '已发送 100%，正在处理末尾音频……');
    this.armTimeout();
    try { this.options.end(this.cancelled); }
    catch (exc) { this.abort(`连接中断，已有结果保留：${exc.message}`); }
  }
  complete(message) {
    clearTimeout(this.timer); this.resolveFrame?.(false); this.resolveFrame = null;
    this.busy = false; this.serverStarted = false;
    this.status('finished', message); this.resolveDone?.(); this.resolveDone = null;
  }
  abort(message) {
    if (!this.busy) return;
    ++this.generation; clearTimeout(this.timer);
    this.resolveFrame?.(false); this.resolveFrame = null;
    this.busy = false; this.serverStarted = false;
    this.status('error', message); this.resolveDone?.(); this.resolveDone = null;
  }
}
window.AudioFileInput = AudioFileInput;
