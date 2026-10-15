"use strict";
/**
 * What a plain test writes to Postgres through node-postgres (pg), and TypeORM, Knex or anything
 * else built on it: each INSERT, UPDATE and DELETE as a step, "DB INSERT bookings", with the
 * columns it set and the ones it left empty (sql.js). Reads are only counted per table. Values are
 * never recorded.
 *
 *   recordPg();            // the project's own pg; or recordPg(require("pg"))
 *
 * assay-evals/jest calls it for every test. Queries outside a recorded plain test, and inside an
 * assayCase(), are left alone: a run's agent checks would count three identical inserts as a loop.
 */
const { summarize } = require("./sql");
const { plainRun } = require("./instrument");

const PATCHED = Symbol.for("assay.pg");

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
    const s = q && summarize(q.text, q.values);
    const result = original.apply(this, arguments);
    if (!s) return result;
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
