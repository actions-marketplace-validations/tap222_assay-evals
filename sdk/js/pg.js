"use strict";
/**
 * What a plain test writes to Postgres through node-postgres (pg), and TypeORM, Knex or anything
 * else built on it: each INSERT, UPDATE and DELETE as a step, "DB INSERT bookings", with the
 * columns it set and the ones it left empty (sql.js). Reads are only counted per table. Values are
 * never recorded; they're kept in memory to check what the test sent reached a row (writes.js).
 *
 *   recordPg();            // the project's own pg; or recordPg(require("pg"))
 *
 * assay-evals/jest calls it for every test. Queries outside a recorded plain test, and inside an
 * assayCase(), are left alone: a run's agent checks would count three identical inserts as a loop.
 */
const fs = require("fs");
const { summarize } = require("./sql");
const { plainRun } = require("./instrument");

const PATCHED = Symbol.for("assay.pg");
const SDK = __dirname;
const TEST_FILE = /\.(spec|test|e2e-spec)\.[cm]?[jt]sx?$|[\\/](__tests__|test|tests)[\\/]/;

const exists = new Map();

/** Whether a stack frame's file is on disk: a library's source map can point at its own src/ paths. */
function onDisk(file) {
  if (!exists.has(file)) {
    let found = false;
    try {
      found = fs.existsSync(file.startsWith("file://") ? new URL(file) : file);
    } catch {
      // not a path
    }
    exists.set(file, found);
  }
  return exists.get(file);
}

/** Whether the test wrote this itself (a fixture in beforeEach, a raw INSERT in the test), rather than
 * the app: the first frame of the project's own (outside node_modules and Assay) is in a test file. */
function byTest() {
  const holder = {};
  const limit = Error.stackTraceLimit;
  Error.stackTraceLimit = 40;
  Error.captureStackTrace(holder, byTest);
  Error.stackTraceLimit = limit;
  for (const line of String(holder.stack).split("\n").slice(1)) {
    const m = /\(?((?:file:\/\/)?[^()\s]+?):\d+:\d+\)?\s*$/.exec(line);
    if (!m || m[1].startsWith("node:") || m[1].includes("node_modules") || m[1].startsWith(SDK) || !onDisk(m[1])) continue;
    return TEST_FILE.test(m[1]);
  }
  return false;
}

/** The project's pg, as the app loads it; null when it isn't installed. */
function projectPg() {
  try {
    return require(require.resolve("pg", { paths: [process.cwd()] }));
  } catch {
    try {
      return require("pg");
    } catch {
      return null;
    }
  }
}

function textOf(config, values) {
  if (typeof config === "string") return { text: config, values: Array.isArray(values) ? values : undefined };
  if (config && typeof config.text === "string") {
    return { text: config.text, values: Array.isArray(config.values) ? config.values : Array.isArray(values) ? values : undefined };
  }
  return null;
}

function record(run, s, error) {
  try {
    if (s.op === "SELECT") run.dbRead(s.table);
    else run.dbWrite(s, error);
  } catch {
    // recording must never break the query
  }
}

/** Patches pg.Client.prototype.query (a Pool's clients are Clients). Returns whether it's patched. */
function recordPg(pg = projectPg()) {
  const Client = pg && pg.Client;
  if (!Client || typeof Client.prototype.query !== "function") return false;
  if (Client.prototype.query[PATCHED]) return true;
  const original = Client.prototype.query;
  function query(config, values, callback) {
    const run = plainRun();
    const q = run && textOf(config, values);
    const full = q && summarize(q.text, q.values, { values: true });
    const result = original.apply(this, arguments);
    if (!full) return result;
    const { columns, ...s } = full; // values stay in memory
    if (s.op !== "SELECT" && byTest()) s.fixture = true;
    try {
      run.dbUsed(full, q.values);
    } catch {
      // recording must never break the query
    }
    if (s.op === "SELECT") {
      record(run, s);
    } else if (result && typeof result.then === "function") {
      result.then(() => record(run, s), (e) => record(run, s, e)); // the caller still gets the rejection
    } else {
      record(run, s); // a callback or a submittable: its outcome isn't waited for
    }
    return result;
  }
  query[PATCHED] = true;
  Client.prototype.query = query;
  return true;
}

module.exports = { recordPg, projectPg };
