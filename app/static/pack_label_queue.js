/* One automatic label job per opened order, shared by all marketplaces.
   Retain Safari's preopened window while marking/readiness is pending. */
window.PackLabelQueue = class {
  constructor({ keyOf, print, closeWindow, now = Date.now, onError = () => {} }) {
    this.keyOf = keyOf;
    this.print = print;
    this.closeWindow = closeWindow;
    this.now = now;
    this.onError = onError;
    this.jobs = new Map();
  }

  request(state, printWindow = null) {
    const key = this.keyOf(state);
    const existing = this.jobs.get(key);
    if (existing) {
      if (!existing.window || existing.window.closed) existing.window = printWindow;
      else if (printWindow && printWindow !== existing.window) this.closeWindow(printWindow);
      return;
    }
    this.jobs.set(key, { window: printWindow, running: false, retryAt: 0, warned: false });
  }

  async flush(state) {
    const key = this.keyOf(state);
    const job = this.jobs.get(key);
    if (!state?.active || !state.sticker_download?.ready || !job || job.running || job.retryAt > this.now()) return false;
    job.running = true;
    try {
      const reserved = job.window && !job.window.closed ? job.window : null;
      const ok = await this.print(state, reserved, () => this.jobs.get(key) === job);
      if (ok && this.jobs.get(key) === job) this.jobs.delete(key);
      return Boolean(ok);
    } catch (error) {
      if (!job.warned) this.onError(error);
      job.warned = true;
      return false;
    } finally {
      job.running = false;
      job.retryAt = this.now() + 10000;
    }
  }

  clear() {
    for (const job of this.jobs.values()) this.closeWindow(job.window);
    this.jobs.clear();
  }

  forget(state) { this.jobs.delete(this.keyOf(state)); }
};
