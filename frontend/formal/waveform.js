// 仅显示麦克风输入：短音量历史从左向右推进，不生成随机或循环波形。
export class MicrophoneWaveform {
  constructor() {
    this.history = Array(35).fill(0);
    this.active = false;
    this.latest = this.lastInput = this.smoothed = 0;
    this.raf = null;
  }
  input(rms, active) {
    const changed = this.active !== active;
    this.active = active;
    this.latest = Number.isFinite(rms) ? Math.max(0,rms) : 0;
    this.lastInput = performance.now();
    if (!active) this.reset();
    if (!active || changed) this.sync();
  }
  bind(element) {
    if (this.element !== element) {
      this.element = element;
      this.bars = element ? [...element.querySelectorAll('i')] : [];
      if (!element) this.reset();
    }
    this.sync();
  }
  reset() {
    this.history.fill(0);
    this.smoothed = 0;
    this.lastTick = this.lastSample = null;
  }
  sync() {
    const running = this.active && !!this.element?.isConnected && !document.hidden;
    this.element?.classList.toggle('active',this.active);
    if (!running) {
      if (this.raf !== null) cancelAnimationFrame(this.raf);
      this.raf = null;
      this.reset();
    }
    this.paint();
    if (running && this.raf === null) this.raf = requestAnimationFrame(time=>this.tick(time));
  }
  tick(time) {
    this.raf = null;
    if (!this.active || !this.element?.isConnected || document.hidden) { this.sync(); return; }
    const elapsed = this.lastTick === null ? 34 : time-this.lastTick;
    if (elapsed >= 32) {
      this.lastTick = time;
      // -50 dB 左右的输入视为底噪；200 ms 无输入时不能停留在旧音量。
      const rms = time-this.lastInput > 200 ? 0 : this.latest;
      const target = Math.min(1,Math.log1p(24*Math.max(0,rms-0.003))/Math.log1p(24*0.15));
      const tau = target > this.smoothed ? 50 : 150;
      this.smoothed += (target-this.smoothed)*(1-Math.exp(-Math.min(elapsed,100)/tau));
      if (this.smoothed < 0.001) this.smoothed = 0;
      if (this.lastSample === null || time-this.lastSample >= 50) {
        this.history.shift(); this.history.push(this.smoothed); this.lastSample = time;
      }
      this.paint();
    }
    this.raf = requestAnimationFrame(next=>this.tick(next));
  }
  paint() {
    this.bars?.forEach((bar,index)=>{bar.style.height=`${(4+33*this.history[index]).toFixed(1)}px`;});
  }
}
