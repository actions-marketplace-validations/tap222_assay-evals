"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test, beforeEach } = require("node:test");

const assay = require("..");
const { summarize } = require("../sql");
const { recordPg } = require("../pg");

let file;
beforeEach(() => {
  file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "assay-js-")), "events.jsonl");
  process.env.ASSAY_PATH = file;
  process.env.ASSAY_TEST_RUN = "t-1";
});
const events = () => fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
const steps = () => events().filter((e) => e.type === "step");

test("a write is its operation, table, the columns it sets and the ones it leaves empty", () => {
  assert.deepEqual(summarize('INSERT INTO "public"."bookings"("id", "amount", "locationId") VALUES (DEFAULT, $1, $2) RETURNING "id"', [500, null]),
                   { op: "INSERT", table: "bookings", set: ["id", "amount", "locationId"], empty: ["id", "locationId"] });
  assert.deepEqual(summarize("insert into clinics (name, timezone, active) values ($1, 'UTC', true)", ["Salon"]),
                   { op: "INSERT", table: "clinics", set: ["name", "timezone", "active"], empty: [] });
  assert.deepEqual(summarize('UPDATE "bookings" SET "status" = $1, "checkedInAt" = NULL WHERE "id" = $2', ["arrived", 7]),
                   { op: "UPDATE", table: "bookings", set: ["status", "checkedInAt"], empty: ["checkedInAt"] });
  assert.deepEqual(summarize('DELETE FROM "queue_entries" WHERE "id" = $1', [1]),
                   { op: "DELETE", table: "queue_entries", set: [], empty: [] });
});

test("a column is empty only when every row leaves it empty", () => {
  assert.deepEqual(summarize("INSERT INTO t (a, b) VALUES ($1, $2), ($3, $4)", [null, 1, null, null]).empty, ["a"]);
});

test("comments, strings and subqueries don't confuse it", () => {
  assert.deepEqual(summarize("-- insert into fake\nUPDATE users SET email = $1 -- the new one\nWHERE id = $2", [null, 2]),
                   { op: "UPDATE", table: "users", set: ["email"], empty: ["email"] });
  assert.deepEqual(summarize("WITH x AS (SELECT msg FROM a) INSERT INTO logs (msg) SELECT msg FROM x", []),
                   { op: "INSERT", table: "logs", set: ["msg"], empty: [] });
  assert.equal(summarize('SELECT "b"."id" FROM "bookings" "b" WHERE "b"."note" = \'insert into x\'').table, "bookings");
});

test("statements that aren't reads or writes, and SQL it can't read, are nothing", () => {
  for (const q of ["START TRANSACTION", "COMMIT", "SELECT 1", "select now()", "", "INSERT INTO", null]) {
    assert.equal(summarize(q), null, String(q));
  }
});

/** A pg-like Client: query() answers with a promise, or calls back. */
function fakePg() {
  class Client {
    query(config, values, callback) {
      const cb = typeof values === "function" ? values : callback;
      const text = typeof config === "string" ? config : config.text;
      if (cb) return void cb(null, { rows: [] });
      return text.includes("broken") ? Promise.reject(Object.assign(new Error("column missing"), { code: "42703" }))
                                     : Promise.resolve({ rows: [] });
    }
  }
  return { Client };
}

test("a plain test's writes are steps, its reads one count per table, and values are never kept", async () => {
  const pg = fakePg();
  assert.equal(recordPg(pg), true);
  assert.equal(recordPg(pg), true); // twice is once
  const client = new pg.Client();
  const run = new assay.Run("books a slot", { caseId: "a.test.js::books a slot", tags: { assay_record: "plain" } });
  assay.setPlainRun(run);
  try {
    await client.query('INSERT INTO "bookings"("amount", "locationId") VALUES ($1, $2)', [500, null]);
    await client.query({ text: "UPDATE bookings SET status = $1 WHERE id = $2", values: ["secret-value", 1] });
    await client.query("SELECT * FROM bookings");
    await client.query("SELECT * FROM bookings WHERE id = $1", [1]);
    await assert.rejects(client.query("INSERT INTO broken (a) VALUES ($1)", [1]));
    await new Promise((resolve) => client.query("DELETE FROM queue_entries WHERE id = $1", [1], resolve));
  } finally {
    assay.setPlainRun(null);
  }
  run.end();
  const s = steps();
  assert.deepEqual(s.map((x) => x.name),
                   ["DB INSERT bookings", "DB UPDATE bookings", "DB INSERT broken", "DB DELETE queue_entries", "DB reads"]);
  assert.deepEqual(s[0].args, { set: ["amount", "locationId"], empty: ["locationId"] });
  assert.equal(s[2].status, "error");
  assert.match(s[2].error, /42703 column missing/);
  assert.deepEqual(s[4].args, { bookings: 2 });
  assert.ok(!fs.readFileSync(file, "utf8").includes("secret-value"));
});

test("queries outside a plain test, or inside an assayCase, aren't recorded", async () => {
  const pg = fakePg();
  recordPg(pg);
  const client = new pg.Client();
  await client.query("INSERT INTO a (x) VALUES (1)"); // no test running
  const plain = new assay.Run("plain", { caseId: "p", tags: { assay_record: "plain" } });
  assay.setPlainRun(plain);
  try {
    await assay.assayCase("agent", async () => { await client.query("INSERT INTO a (x) VALUES (1)"); });
  } finally {
    assay.setPlainRun(null);
  }
  assert.deepEqual(steps().filter((x) => x.name.startsWith("DB ")), []);
});
