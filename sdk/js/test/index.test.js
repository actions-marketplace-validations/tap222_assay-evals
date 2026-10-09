"use strict";
const assert = require("node:assert/strict");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { test, beforeEach } = require("node:test");

const assay = require("..");

let file;
beforeEach(() => {
  file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), "assay-js-")), "events.jsonl");
  process.env.ASSAY_PATH = file;
  process.env.ASSAY_TEST_RUN = "t-1";
  delete process.env.ASSAY_TEST_ATTEMPT;
});
const events = () => fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));

test("a case records its steps, the test's result and its end, in the form the server takes", async () => {
  const ref = assay.prompt("support", 2, "Refunds need approval first.");
  await assay.assayCase("refund", async (run) => {
    run.expect().mustCall("get_order").maxSteps(5);
    const order = await run.call("get_order", async ({ orderId }) => ({ orderId, price: 27.61 }), { orderId: "O-17" });
    run.llm({ model: "m", tokensIn: 850, tokensOut: 60, prompt: ref });
    run.answer(`Refunded $${order.price}.`);
    run.outcome("resolved");
  });
  const ev = events();
  assert.deepEqual(ev.map((e) => e.type + (e.kind ? `:${e.kind}` : "")),
    ["prompt", "run.start:agent", "step:tool", "step:llm", "step:answer", "check", "check", "check", "run.end"]);
  assert.ok(ev.every((e) => e.v === 1 && /^[0-9a-f]{32}$/.test(e.id) && e.ts.endsWith("Z")));
  const start = ev[1];
  assert.deepEqual(start.test, { case: "refund", run: "t-1" });
  assert.deepEqual(ev[2], { ...ev[2], name: "get_order", args: { orderId: "O-17" }, status: "ok", seq: 0 });
  assert.equal(ev[3].prompt, "support@2");
  assert.deepEqual(ev.filter((e) => e.type === "check").map((e) => [e.field, e.status]),
    [["expect.must_call(get_order)", "pass"], ["expect.max_steps(5)", "pass"], ["test", "pass"]]);
  assert.equal(ev.at(-1).outcome, "resolved");
  assert.ok(ev.slice(1).every((e) => e.type === "prompt" || e.run_id === start.run_id));
});

test("a failed expectation fails the test, and is recorded as itself", async () => {
  await assert.rejects(assay.assayCase("no lookup", async (run) => {
    run.expect().mustCall("get_order");
    run.answer("Done.");
  }), /must_call\(get_order\): Expected a call to get_order\(\)/);
  const checks = events().filter((e) => e.type === "check");
  assert.deepEqual(checks.map((c) => [c.field, c.status]), [["expect.must_call(get_order)", "fail"], ["test", "pass"]]);
});

test("an assert that throws fails the case and the run", async () => {
  await assert.rejects(assay.assayCase("boom", async () => { throw new Error("expected 2, got 3"); }), /got 3/);
  const ev = events();
  assert.deepEqual(ev.find((e) => e.field === "test"), { ...ev.find((e) => e.field === "test"), status: "fail",
                                                         reason: "expected 2, got 3" });
  assert.equal(ev.at(-1).status, "failed");
});

test("a tool that throws is recorded as an error, then rethrown", async () => {
  await assert.rejects(assay.assayCase("down", async (run) => {
    await run.call("get_order", async () => { throw new TypeError("network down"); });
  }), /network down/);
  const step = events().find((e) => e.type === "step");
  assert.equal(step.status, "error");
  assert.equal(step.error, "TypeError: network down");
});

test("with no ASSAY_PATH nothing is written, and the run is still there for asserts", async () => {
  delete process.env.ASSAY_PATH;
  const steps = await assay.assayCase("quiet", async (run) => {
    run.tool("lookup", { q: 1 }, { ok: true });
    return run.steps;
  });
  assert.equal(steps.length, 1);
  assert.equal(fs.existsSync(file), false);
});

test("repeats carry their attempt, and long text is cut", async () => {
  process.env.ASSAY_TEST_ATTEMPT = "2";
  await assay.assayCase("long", async (run) => run.answer("x".repeat(30000)));
  const ev = events();
  assert.equal(ev[0].test.attempt, 2);
  assert.equal(ev.find((e) => e.kind === "answer").text.length, 20000);
});

test("a long case id keeps its start and a hash of the whole, as the Python plugin does", () => {
  const long = "src/a/b.spec.ts::" + "x".repeat(200);
  const id = assay.caseIdOf(long);
  assert.equal(id.length, 128);
  assert.ok(id.startsWith("src/a/b.spec.ts::xxx") && /~[0-9a-f]{8}$/.test(id));
  assert.equal(assay.caseIdOf("short"), "short");
});
