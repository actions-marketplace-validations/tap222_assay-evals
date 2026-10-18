"use strict";
/**
 * The JSON a plain test sends to the app in the same process (supertest, or http to localhost), so
 * a value in it that no query used can be reported (writes.js). Bodies are read in memory and never
 * recorded. Requests to other hosts, and requests outside a recorded plain test, are left alone.
 *
 *   recordHttp();   // assay-evals/jest calls it
 */
const http = require("http");
const { plainRun } = require("./instrument");

const PATCHED = Symbol.for("assay.http");
const LOCAL = new Set(["localhost", "127.0.0.1", "::1", "[::1]", "0.0.0.0"]);
const WRITES = new Set(["POST", "PUT", "PATCH"]);
const MAX_BODY = 1 << 20;
const bodies = new WeakMap();

function wanted(req) {
  return WRITES.has(String(req.method).toUpperCase()) && LOCAL.has(String(req.host || "").toLowerCase()) &&
         /json/i.test(String(req.getHeader("content-type") || ""));
}

/** The path without its query, and ids (numbers, UUIDs, long hex or tokens) as :id: values aren't recorded. */
function route(path) {
  return String(path).split("?")[0].split("/").map((seg) =>
    /^\d+$|^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$|^[0-9a-f]{16,}$|^(?=.*\d)[\w-]{20,}$/i.test(seg)
      ? ":id" : seg).join("/");
}

function keep(req, chunk, encoding) {
  if (chunk === undefined || chunk === null || typeof chunk === "function") return;
  const parts = bodies.get(req) || [];
  const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(String(chunk), typeof encoding === "string" ? encoding : "utf8");
  if (parts.reduce((n, b) => n + b.length, 0) + buf.length <= MAX_BODY) parts.push(buf);
  bodies.set(req, parts);
}

/** Patches http.ClientRequest's write and end. Returns whether it's patched. */
function recordHttp(ClientRequest = http.ClientRequest) {
  const proto = ClientRequest && ClientRequest.prototype;
  if (!proto || typeof proto.end !== "function") return false;
  if (proto.end[PATCHED]) return true;
  const write = proto.write, end = proto.end;
  proto.write = function (chunk, encoding) {
    try {
      if (plainRun() && wanted(this)) keep(this, chunk, encoding);
    } catch {
      // recording must never break the request
    }
    return write.apply(this, arguments);
  };
  proto.end = function (chunk, encoding) {
    try {
      const run = plainRun();
      if (run && wanted(this)) {
        keep(this, chunk, encoding);
        const text = Buffer.concat(bodies.get(this) || []).toString("utf8");
        bodies.delete(this);
        if (text.trim()) run.sent(JSON.parse(text), `${this.method.toUpperCase()} ${route(this.path)}`);
      }
    } catch {
      // a body that isn't JSON, or anything else: the request goes on as it was
    }
    return end.apply(this, arguments);
  };
  proto.end[PATCHED] = true;
  return true;
}

module.exports = { recordHttp, route };
