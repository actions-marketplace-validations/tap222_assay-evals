"use strict";
const assert = require("node:assert/strict");
const { test, beforeEach } = require("node:test");

const { Sender, project } = require("../send");

// A server that records what it's sent; answers with the statuses given, then 200.
function server(...statuses) {
  const calls = [];
  const fetchFn = async (url, init) => {
    calls.push({ url, headers: init.headers, events: JSON.parse(init.body).events });
    const status = statuses.length ? statuses.shift() : 200;
    if (status === "down") throw new Error("ECONNREFUSED");
    return { ok: status < 300, status, text: async () => "detail" };
  };
  return { calls, fetchFn };
}

let posted;
beforeEach(() => {
  delete process.env.ASSAY_PATH;
  delete process.env.ASSAY_KEY;
  delete process.env.ASSAY_PROJECT;
  process.env.ASSAY_URL = "http://assay.test:8400/";
  posted = [];
  globalThis.fetch = async (url, init) => {
    posted.push({ url, headers: init.headers, events: JSON.parse(init.body).events });
    return { ok: true, status: 200, text: async () => "" };
  };
});

test("a sender posts its events to /v1/ingest, in order, with the key and project", async () => {
  const { calls, fetchFn } = server();
  const s = new Sender("http://assay.test/", { key: "ak_1", tenant: "inqloop", fetchFn });
  s.add({ type: "run.start", run_id: "r" });
  s.add({ type: "run.end", run_id: "r" });
  assert.equal(await s.flush(), true);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "http://assay.test/v1/ingest");
  assert.equal(calls[0].headers.Authorization, "Bearer ak_1");
  assert.equal(calls[0].headers["X-Tenant"], "inqloop");
  assert.deepEqual(calls[0].events.map((e) => e.type), ["run.start", "run.end"]);
});

test("a server that's down keeps the events, in order, for the next try", async () => {
  const { calls, fetchFn } = server("down", 503);
  const s = new Sender("http://assay.test", { fetchFn });
  s.add({ type: "run.start", run_id: "r" });
  assert.equal(await s.flush(), false);
  assert.equal(await s.flush(), false);
  s.add({ type: "run.end", run_id: "r" });
  assert.equal(await s.flush(), true);
  assert.deepEqual(calls.at(-1).events.map((e) => e.type), ["run.start", "run.end"]);
  assert.equal(s.queue.length, 0);
  clearTimeout(s.timer);
});

test("a batch the server rejects as malformed is dropped, not retried forever", async () => {
  const { calls, fetchFn } = server(422);
  const s = new Sender("http://assay.test", { fetchFn });
  s.add({ type: "bogus" });
  assert.equal(await s.flush(), true);
  assert.equal(s.queue.length, 0);
  assert.equal(calls.length, 1);
});

test("the project is ASSAY_PROJECT, else the folder's name; with a key, the key's own", () => {
  process.env.ASSAY_PROJECT = "Booking CRM Backend";
  assert.equal(project(), "booking-crm-backend");
  delete process.env.ASSAY_PROJECT;
  assert.equal(project(), require("path").basename(process.cwd()).toLowerCase().replace(/[^a-z0-9._-]+/g, "-"));
  process.env.ASSAY_KEY = "ak_1";
  assert.equal(project(), "");
});

test("track() sends the app's run with its model calls, tokens and cost to ASSAY_URL", async () => {
  process.env.ASSAY_PROJECT = "inqloop";
  const assay = require("..");
  const client = assay.instrument({
    messages: {
      create: async (p) => ({ model: p.model, stop_reason: "end_turn", content: [{ type: "text", text: "Rs 500." }],
                              usage: { input_tokens: 900, output_tokens: 70 } }),
    },
  });
  const reply = await assay.track("whatsapp_reply", async (run) => {
    await run.call("detect_intent", async () => "pricing", { message: "How much?" });
    const r = await client.messages.create({ model: "claude-opus-5", max_tokens: 100, messages: [] });
    run.answer(r.content[0].text);
    run.outcome("resolved");
    return r.content[0].text;
  }, { input: "How much?", conversationId: "c-1", turn: 0 });
  assert.equal(reply, "Rs 500.");
  assert.equal(await assay.flush(), true);
  const ev = posted.flatMap((p) => p.events);
  assert.equal(posted[0].url, "http://assay.test:8400/v1/ingest");
  assert.equal(posted[0].headers["X-Tenant"], "inqloop");
  assert.deepEqual(ev.map((e) => e.type + (e.kind ? `:${e.kind}` : "")),
    ["run.start:agent", "step:tool", "step:llm", "step:answer", "run.end"]);
  assert.equal(ev[0].task, "whatsapp_reply");
  assert.equal(ev[0].input, "How much?");
  assert.equal(ev[0].conversation_id, "c-1");
  assert.equal(ev[0].test, undefined);
  assert.equal(ev[2].model, "claude-opus-5");
  assert.equal(ev[2].tokens_in, 900);
  assert.equal(ev.at(-1).outcome, "resolved");
  assert.equal(ev.at(-1).status, "completed");
});

test("track() ends a run failed when the request throws, and rethrows", async () => {
  const assay = require("..");
  await assert.rejects(assay.track("whatsapp_reply", async () => { throw new Error("db down"); }), /db down/);
  await assay.flush();
  const end = posted.flatMap((p) => p.events).at(-1);
  assert.equal(end.type, "run.end");
  assert.equal(end.status, "failed");
  assert.match(end.error, /db down/);
});

test("under assay test (ASSAY_PATH set) nothing is sent", async () => {
  const fs = require("node:fs"), os = require("node:os"), path = require("node:path");
  process.env.ASSAY_PATH = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "assay-js-")), "events.jsonl");
  const assay = require("..");
  await assay.track("t", async (run) => run.answer("ok"));
  await assay.flush();
  assert.equal(posted.length, 0);
  assert.equal(fs.readFileSync(process.env.ASSAY_PATH, "utf8").trim().split("\n").length, 3);
});
