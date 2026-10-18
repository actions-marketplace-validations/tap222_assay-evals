"use strict";
/**
 * Values a test gave the code that never reached the database. A request sets a booking's branch,
 * and the row saved has no branch: nothing fails, the booking just lands in the wrong place.
 *
 * What the test sent (a JSON body to the app in the same process, or assay.sent(input)) and the
 * values each query used are kept in memory while the test runs. When it ends, each sent value is
 * looked for among the values of the queries that ran after it:
 *
 *   never  no query used it
 *   json   it reached the database only inside a JSON column, e.g. bookings.metadata
 *
 * Only field names are recorded, as the step "DB unwritten"; values never are. A sent value that
 * no write followed (a request the app rejected) isn't checked.
 */

const MAX_LEAVES = 200;
const MAX_USED = 5000;
// Fields an app is expected to transform before saving, or not save at all.
const SKIP = /pass(word)?|secret|token|otp|captcha|confirm/i;

/** [path, value] for each value in input worth looking for: text of two characters or more, and
 * numbers other than 0 and 1. */
function leaves(input, prefix = "", out = []) {
  if (out.length >= MAX_LEAVES || input === null || input === undefined) return out;
  if (input instanceof Date) {
    if (!Number.isNaN(input.getTime())) out.push([prefix || "value", input]);
  } else if (Array.isArray(input)) {
    input.forEach((v, i) => leaves(v, `${prefix}[${i}]`, out));
  } else if (typeof input === "object") {
    for (const [k, v] of Object.entries(input)) {
      if (!SKIP.test(k)) leaves(v, prefix ? `${prefix}.${k}` : k, out);
    }
  } else if ((typeof input === "string" && input.trim().length >= 2) ||
             (typeof input === "number" && Number.isFinite(input) && input !== 0 && input !== 1)) {
    out.push([prefix || "value", input]);
  }
  return out;
}

/** A value as the database would compare it: a number is the same as its text, a Date its ISO time. */
function norm(v) {
  if (v instanceof Date) return Number.isNaN(v.getTime()) ? null : v.toISOString();
  if (typeof v === "number" || typeof v === "bigint") return String(Number(v));
  if (typeof v === "string") {
    const t = v.trim();
    return /^-?\d+(\.\d+)?$/.test(t) ? String(Number(t)) : t;
  }
  if (typeof v === "boolean") return String(v);
  return null;
}

/** Whether v, or any value inside it when it's an object, array or JSON text, is `want`. */
function inside(v, want, depth = 0) {
  if (depth > 20 || v === null || v === undefined) return false;
  if (typeof v === "string" && /^\s*[[{]/.test(v)) {
    try {
      return inside(JSON.parse(v), want, depth + 1);
    } catch {
      return false;
    }
  }
  if (Array.isArray(v)) return v.some((x) => inside(x, want, depth + 1));
  if (typeof v === "object" && !(v instanceof Date)) return Object.values(v).some((x) => inside(x, want, depth + 1));
  return norm(v) === want;
}

/** Keeps what a test sent and what its queries used, and says what never got through. */
class Tracker {
  constructor() {
    this.sent = [];   // { label, path, value, at, writes }
    this.used = [];   // { value, column }: column "table.col" for a written value, else null
    this.writes = 0;
  }

  /** input: what the code under test was given; label: where, e.g. "POST /bookings". */
  send(input, label) {
    for (const [path, value] of leaves(input)) {
      this.sent.push({ label, path, value: norm(value), at: this.used.length, writes: this.writes });
    }
  }

  /** A query: s from summarize(..., { values: true }), and the values it was bound to. */
  query(s, values) {
    if (this.used.length >= MAX_USED) return;
    if (s && s.op !== "SELECT") {
      this.writes++;
      for (const [column, value] of s.columns || []) this.used.push({ value, column: `${s.table}.${column}` });
    }
    for (const value of Array.isArray(values) ? values : []) this.used.push({ value, column: null });
  }

  /** { label: { never: [path], json: { path: "table.col" } } } for what didn't get through; null if all did. */
  unwritten() {
    const out = {};
    for (const x of this.sent) {
      if (this.writes === x.writes) continue; // nothing was written after it
      const after = this.used.slice(x.at);
      if (after.some((u) => (typeof u.value !== "object" || u.value instanceof Date) && norm(u.value) === x.value)) continue;
      const json = after.find((u) => u.column && typeof u.value !== "string" && inside(u.value, x.value)) ||
                   after.find((u) => u.column && inside(u.value, x.value));
      const where = out[x.label] = out[x.label] || {};
      if (json) (where.json = where.json || {})[x.path] = json.column;
      else if (!after.some((u) => inside(u.value, x.value))) (where.never = where.never || []).push(x.path);
    }
    for (const k of Object.keys(out)) if (!Object.keys(out[k]).length) delete out[k];
    return Object.keys(out).length ? out : null;
  }
}

module.exports = { Tracker, leaves };
