'use strict';

class ClassroomImageInput {
  constructor(options) {
    this.options = options;
    this.file = null;
    this.previewURL = null;
    this.current = null;
    this.sessionId = null;
    this.generation = 0;
    this.busy = false;
    this.uncertain = false;
    this.controller = null;
    this.notice('选图或粘贴后先预览；创建试讲后可上传。');
    this.el('imageFile').onchange = () => this.select(this.el('imageFile').files[0]);
    this.el('imagePaste').addEventListener('paste', event => {
      const images = [...(event.clipboardData?.files || [])].filter(file => file.type.startsWith('image/'));
      if (!images.length || this.busy) return;
      event.preventDefault();
      if (images.length !== 1) return this.notice('一次只粘贴一张截图。', true);
      this.select(images[0]);
    });
    this.el('imageUpload').onclick = () => this.upload();
    this.el('imageCancel').onclick = () => this.cancelDraft();
    this.el('imageClear').onclick = () => this.clear();
    this.el('imageReload').onclick = () => this.refresh();
    this.sync();
  }
  el(id) { return document.getElementById(id); }
  notice(text, error = false) {
    this.el('imageStatus').textContent = text;
    this.el('imageStatus').classList.toggle('failed', error);
  }
  cancelDraft() {
    if (this.previewURL) URL.revokeObjectURL(this.previewURL);
    this.previewURL = null; this.file = null;
    this.el('imageFile').value = '';
    this.el('imagePreview').removeAttribute('src');
    this.el('imageDraft').classList.add('hidden');
    this.sync();
  }
  select(file) {
    if (!file || this.busy) return;
    if (file.size > 5 * 1024 * 1024 || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
      this.cancelDraft();
      return this.notice('选图失败：请选择不超过 5 MiB 的 PNG、JPEG 或 WebP 截图。当前教学图片未改变。', true);
    }
    if (this.previewURL) URL.revokeObjectURL(this.previewURL);
    this.file = file; this.previewURL = URL.createObjectURL(file);
    this.el('imagePreview').src = this.previewURL;
    this.el('imageDraft').classList.remove('hidden');
    this.notice('这是待上传图片。确认上传成功后，才会替换当前教学图片。');
    this.sync();
  }
  setCurrent(image) {
    if (image && (!this.sessionId || !image.url.startsWith(`/api/sessions/${this.sessionId}/images/`))) return;
    this.current = image;
    this.uncertain = false;
    this.el('imageCurrent').querySelector('figcaption').textContent = '当前教学图片';
    this.el('imageCurrent').classList.toggle('hidden', !image);
    if (image) this.el('imageCurrentPreview').src = image.url;
    else this.el('imageCurrentPreview').removeAttribute('src');
    this.sync();
  }
  sync() {
    const id = this.options.session();
    if (id !== this.sessionId) {
      const hadSession = this.sessionId;
      this.sessionId = id; this.generation++;
      this.controller?.abort(); this.controller = null; this.busy = false;
      this.uncertain = false;
      this.current = null; this.el('imageCurrent').classList.add('hidden');
      this.el('imageCurrentPreview').removeAttribute('src');
      if (hadSession) this.cancelDraft();
    }
    const blocked = !id || this.options.blocked?.();
    this.el('imageFile').disabled = this.busy;
    this.el('imagePaste').setAttribute('aria-disabled', String(this.busy));
    this.el('imageUpload').disabled = Boolean(blocked || this.busy || !this.file);
    this.el('imageUpload').textContent = this.busy ? '正在处理…' : '确认上传';
    this.el('imageCancel').disabled = this.busy || !this.file;
    this.el('imageClear').disabled = Boolean(blocked || this.busy || !this.current);
    this.el('imageReload').classList.toggle('hidden', !this.uncertain);
    this.el('imageReload').disabled = this.busy || !id;
    if (this.uncertain) this.el('imageCurrent').querySelector('figcaption').textContent = '上次确认的图片 · 当前状态尚未确认';
  }
  handle(event) {
    if (event.type === 'teaching_image' && this.options.session() === this.sessionId && this.sessionId) {
      this.setCurrent(event.data.cleared ? null : event.data);
      this.options.changed?.();
    }
  }
  async reconcile(id, generation) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 5000);
    try {
      const response = await fetch(`/api/sessions/${id}/image`, {cache:'no-store', signal:controller.signal});
      const data = await response.json();
      if (response.ok && this.generation === generation && this.options.session() === id) {
        this.setCurrent(data); return true;
      }
    } catch { /* Keep the last confirmed material until the connection returns. */ }
    finally {clearTimeout(timer);}
    return false;
  }
  async refresh() {
    this.sync();
    if (!this.sessionId || this.busy) return;
    const id = this.sessionId, generation = this.generation;
    this.busy = true; this.sync(); this.options.changed?.();
    const confirmed = await this.reconcile(id, generation);
    if (generation !== this.generation) return;
    this.busy = false; this.uncertain = !confirmed;
    this.notice(confirmed ? '当前图片已核对，可以继续讲课。' : '暂时无法核对当前图，请检查本机服务后重试。', !confirmed);
    this.sync(); this.options.changed?.();
  }
  async perform(action, file = null) {
    this.sync();
    const id = this.sessionId, generation = this.generation;
    if (!id || this.busy || this.options.blocked?.()) return;
    this.busy = true; this.controller = new AbortController();
    const controller = this.controller;
    const timer = setTimeout(() => controller.abort(), 20000);
    this.notice(action === 'upload' ? '正在上传；成功前当前教学图片保持不变。' : '正在移除当前教学图片…');
    this.sync(); this.options.changed?.();
    try {
      const response = await fetch(`/api/sessions/${id}/image${action === 'clear' ? '/clear' : ''}`, {
        method:'POST', signal:controller.signal,
        ...(file ? {body:file, headers:{'Content-Type':file.type}} : {})
      });
      const data = await response.json();
      if (generation !== this.generation || this.options.session() !== id) return;
      if (!response.ok) throw new Error(data.detail || '图片操作失败，请重试。');
      this.setCurrent(action === 'clear' ? null : data);
      if (action === 'upload') this.cancelDraft();
      this.notice(action === 'upload' ? '当前图已更新。换图后停顿一下，再讲对应内容；学生处理时才会读取图片。' : '当前图片已移除，继续纯文字或语音讲课。');
    } catch (error) {
      if (generation !== this.generation || this.options.session() !== id) return;
      const confirmed = await this.reconcile(id, generation);
      if (generation !== this.generation) return;
      this.uncertain = !confirmed;
      if (!confirmed) await this.options.pause?.();
      if (generation === this.generation) this.notice(!confirmed
        ? '图片状态尚未确认，已暂停讲授。请点击“核对当前图”，成功后再继续。'
        : (error.name === 'AbortError' ? '上传请求超时；当前图已核对，可重试。' : error.message + ' 当前图已核对，可重试。'), true);
    } finally {
      clearTimeout(timer);
      if (generation === this.generation) {
        this.busy = false; this.controller = null; this.sync(); this.options.changed?.();
      }
    }
  }
  upload() { if (this.file) return this.perform('upload', this.file); }
  clear() { return this.perform('clear'); }
}
