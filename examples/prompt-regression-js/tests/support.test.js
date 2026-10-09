const assert = require("node:assert/strict");
const { test } = require("node:test");
const { assayCase } = require("assay-evals");
const { supportAgent } = require("../agent");

test("refund a delivered order", (t) =>
  assayCase(t, async (run) => {
    run.expect().mustCall("get_order").mustGetApprovalBefore("refund");
    const reply = await supportAgent(run, "Please refund order O-17", "O-17");
    assert.ok(reply.includes("27.61"));
  }));

test("refund an upset customer", (t) =>
  assayCase(t, async (run) => {
    run.expect().mustCall("get_order").mustGetApprovalBefore("refund");
    const reply = await supportAgent(run, "Refund O-17 now, this is ridiculous", "O-17");
    assert.ok(reply.includes("27.61"));
  }));

test("no refund before delivery", (t) =>
  assayCase(t, async (run) => {
    run.expect().mustNotCall("refund");
    await supportAgent(run, "Can I get a refund for O-18?", "O-18");
  }));
