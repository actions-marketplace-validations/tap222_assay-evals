"use strict";
/**
 * Events from the app itself, sent to the Assay server in the background: POST /v1/ingest, in
 * batches, in order, retried with backoff. Used when ASSAY_URL is set and ASSAY_PATH isn't (under
 * `assay test` events go to ASSAY_PATH instead). Nothing here ever throws into the app: a server
 * that's down costs a warning and, once the queue is full, the oldest events.
 *
 *   ASSAY_URL      the server, e.g. http://localhost:8400
 *   ASSAY_KEY      an API key with the ingest scope, when the server asks for one
 *   ASSAY_PROJECT  the project its runs are filed under (default: the key's own, else the folder's name)
 */
const path = require("path");

const BATCH = 500;
const MAX_QUEUE = 10000;
const INTERVAL_MS = 1000;
const MAX_BACKOFF_MS = 60000;

function project() {
  const name = process.env.ASSAY_PROJECT || (process.env.ASSAY_KEY ? "" : path.basename(process.cwd()));
  return name.toLowerCase().replace(/[^a-z0-9._-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 48);
}

function warn(message) {
  try {
    process.emitWarning(message, { code: "ASSAY" });
  } catch {
    // a warning must never break the app either
  }
}

class Sender {
  constructor(url, { key, tenant, fetchFn = globalThis.fetch } = {}) {
    this.url = url.replace(/\/+$/, "") + "/v1/ingest";
    this.headers = { "Content-Type": "application/json", "User-Agent": "assay-evals-js" };
    if (key) this.headers.Authorization = `Bearer ${key}`;
    if (tenant) this.headers["X-Tenant"] = tenant;
    this.fetch = fetchFn;
    this.queue = [];
    this.dropped = 0;
    this.sending = null;
    this.timer = null;
    this.backoff = INTERVAL_MS;
  }

  add(event) {
    if (this.queue.length >= MAX_QUEUE) {
      this.queue.shift();
      this.dropped += 1;
      if ([1, 100, 10000].includes(this.dropped)) warn(`Assay: queue full, dropped ${this.dropped} events so far (is ${this.url} reachable?)`);
    }
    this.queue.push(event);
    if (this.queue.length >= BATCH) this.flush();
    else this.schedule(INTERVAL_MS);
  }

  schedule(ms) {
    if (this.timer) return;
    this.timer = setTimeout(() => { this.timer = null; this.flush(); }, ms);
    if (typeof this.timer.unref === "function") this.timer.unref(); // never keeps the process alive
  }

  /** Sends everything queued, one batch at a time; resolves true once the queue is empty. */
  flush() {
    if (this.sending) return this.sending.then(() => this.queue.length ? this.flush() : true);
    if (!this.queue.length) return Promise.resolve(true);
    this.sending = this.drain().finally(() => { this.sending = null; });
    return this.sending;
  }

  async drain() {
    while (this.queue.length) {
      const batch = this.queue.slice(0, BATCH);
      const sent = await this.post(batch);
      if (!sent) { // kept, in order, for the next try
        this.schedule(this.backoff);
        this.backoff = Math.min(this.backoff * 2, MAX_BACKOFF_MS);
        return false;
      }
      this.queue.splice(0, batch.length);
      this.backoff = INTERVAL_MS;
    }
    return true;
  }

  /** True when the batch is done with: sent, or rejected as malformed (retrying won't help). */
  async post(batch) {
    try {
      const r = await this.fetch(this.url, { method: "POST", headers: this.headers, body: JSON.stringify({ events: batch }) });
      if (r.ok) return true;
      const body = await r.text().catch(() => "");
      if (r.status === 422) {
        warn(`Assay rejected ${batch.length} events: ${body.slice(0, 1000)}`);
        return true;
      }
      if (r.status < 500 && r.status !== 429) {
        warn(`Assay: ${this.url} answered ${r.status}: ${body.slice(0, 300)}`);
        return true; // a bad key or project won't fix itself; don't hold the queue on it
      }
      return false;
    } catch (e) {
      if (this.backoff === INTERVAL_MS) warn(`Assay: couldn't reach ${this.url}: ${e && e.message ? e.message : e}`);
      return false;
    }
  }
}

let current = null;
let currentKey = "";

/** The sender for ASSAY_URL, or null when it isn't set. Made again if ASSAY_URL, ASSAY_KEY or ASSAY_PROJECT change. */
function sender() {
  const url = process.env.ASSAY_URL;
  if (!url || typeof globalThis.fetch !== "function") return null;
  const key = [url, process.env.ASSAY_KEY || "", process.env.ASSAY_PROJECT || ""].join("\n");
  if (!current || key !== currentKey) {
    if (current) current.flush();
    current = new Sender(url, { key: process.env.ASSAY_KEY, tenant: project() });
    currentKey = key;
  }
  return current;
}

// What's still queued is sent before a normal exit.
process.once("beforeExit", () => { if (current) current.flush(); });

module.exports = { sender, Sender, project };
