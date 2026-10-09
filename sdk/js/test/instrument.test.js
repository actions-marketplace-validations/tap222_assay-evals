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
  delete process.env.ASSAY_PRICES;
});
const events = () => fs.readFileSync(file, "utf8").trim().split("\n").map((l) => JSON.parse(l));
const steps = () => events().filter((e) => e.type === "step");

// A promise like the SDKs return: a Promise with extras.
function apiPromise(value) {
  const p = Promise.resolve(value);
  p.withResponse = async () => ({ data: value, response: { status: 200 } });
  return p;
}

const TOOLS = [{ name: "get_order", description: "Look up an order.",
                 input_schema: { type: "object", properties: { order_id: { type: "string" } } } }];

test("an instrumented Anthropic client records each call on the case, in order", async () => {
  const sent = [];
  const client = assay.instrument({ messages: { create: (params) => { sent.push(params); return apiPromise({
    model: "claude-opus-5-5", stop_reason: "tool_use",
    content: [{ type: "text", text: "Looking it up." }, { type: "tool_use", id: "t1", name: "get_order", input: { order_id: "O-17" } }],
    usage: { input_tokens: 1200, output_tokens: 40, cache_read_input_tokens: 1000 } }); } } });
  process.env.ASSAY_PRICES = JSON.stringify({ "claude-opus-5": [5, 25, 0.5] });
  await assay.assayCase("anthropic", async (run) => {
    const resp = await client.messages.create({ model: "claude-opus-5-5", max_tokens: 100, tools: TOOLS, messages: [] });
    assert.equal(resp.stop_reason, "tool_use");
    run.answer("done");
  });
  const [llm, answer] = steps();
  assert.equal(llm.kind, "llm");
  assert.equal(answer.kind, "answer"); // recorded before the await resumed
  assert.deepEqual({ model: llm.model, in: llm.tokens_in, out: llm.tokens_out, cached: llm.tokens_cached,
                     finish: llm.finish_reason, text: llm.text },
                   { model: "claude-opus-5-5", in: 1200, out: 40, cached: 1000, finish: "tool_call", text: "Looking it up." });
  assert.deepEqual(llm.tool_calls, [{ name: "get_order", arguments: { order_id: "O-17" }, id: "t1" }]);
  assert.deepEqual(llm.tools, ["get_order"]);
  assert.equal(llm.tool_schemas.get_order["x-assay-description"], "Look up an order.");
  assert.equal(llm.cost_usd, (1200 * 5 + 1000 * 0.5 + 40 * 25) / 1e6); // the longest prefix's price
  assert.equal(sent.length, 1);
});

test("OpenAI chat completions and responses are read into the same shape", async () => {
  const client = assay.instrument({
    chat: { completions: { create: async () => ({ model: "gpt-x", choices: [{ finish_reason: "tool_calls", message: {
      content: null, tool_calls: [{ id: "c1", function: { name: "get_order", arguments: "{\"order_id\":\"O-2\"}" } }] } }],
      usage: { prompt_tokens: 300, completion_tokens: 20, prompt_tokens_details: { cached_tokens: 100 } } }) } },
    responses: { create: async () => ({ model: "gpt-x", status: "completed", output_text: "Hi.",
      output: [{ type: "message", content: [{ type: "output_text", text: "Hi." }] }],
      usage: { input_tokens: 50, output_tokens: 5, output_tokens_details: { reasoning_tokens: 3 } } }) },
  });
  await assay.assayCase("openai", async () => {
    await client.chat.completions.create({ model: "gpt-x", messages: [] });
    await client.responses.create({ model: "gpt-x", input: "hi" });
  });
  const [chat, resp] = steps();
  assert.deepEqual(chat.tool_calls, [{ name: "get_order", arguments: { order_id: "O-2" }, id: "c1" }]);
  assert.equal(chat.finish_reason, "tool_call");
  assert.equal(chat.tokens_cached, 100);
  assert.deepEqual({ text: resp.text, finish: resp.finish_reason, reasoning: resp.tokens_reasoning },
                   { text: "Hi.", finish: "stop", reasoning: 3 });
});

test("a failed call is recorded as an error and still throws; streams and calls outside a case aren't recorded", async () => {
  const client = assay.instrument({ messages: { create: (p) => (p.fail ? Promise.reject(new TypeError("overloaded"))
                                                                     : apiPromise({ model: "m", content: [], usage: {} })) } });
  await assert.rejects(assay.assayCase("errors", async () => {
    await client.messages.create({ model: "m", stream: true });
    await client.messages.create({ model: "m", fail: true });
  }), /overloaded/);
  const llm = steps().filter((s) => s.kind === "llm");
  assert.equal(llm.length, 1);
  assert.deepEqual({ status: llm[0].status, error: llm[0].error, finish: llm[0].finish_reason },
                   { status: "error", error: "TypeError: overloaded", finish: "error" });
  fs.rmSync(file);
  await client.messages.create({ model: "m" }); // outside a case: the app's own call
  assert.equal(fs.existsSync(file), false);
  assert.equal(assay.instrument(client), client); // twice: unchanged
  assert.deepEqual((await client.messages.create({ model: "m" }).withResponse()).response, { status: 200 });
});

test("the Vercel AI SDK middleware records generate calls, current and older result shapes", async () => {
  const mw = assay.assayMiddleware();
  await assay.assayCase("ai-sdk", async () => {
    await mw.wrapGenerate({ model: { modelId: "gpt-x" }, params: { tools: [{ type: "function", name: "get_order",
      description: "Look up an order.", inputSchema: { type: "object" } }] },
      doGenerate: async () => ({ finishReason: "tool-calls",
        content: [{ type: "tool-call", toolCallId: "a1", toolName: "get_order", input: "{\"order_id\":\"O-9\"}" }],
        usage: { inputTokens: 400, outputTokens: 30, cachedInputTokens: 200 } }) });
    await mw.wrapGenerate({ model: { modelId: "old-model" }, params: {},
      doGenerate: async () => ({ text: "Hello.", finishReason: "stop", usage: { promptTokens: 10, completionTokens: 2 } }) });
  });
  const [a, b] = steps();
  assert.deepEqual({ model: a.model, tools: a.tools, calls: a.tool_calls, finish: a.finish_reason, in: a.tokens_in, cached: a.tokens_cached },
                   { model: "gpt-x", tools: ["get_order"], calls: [{ name: "get_order", arguments: { order_id: "O-9" }, id: "a1" }],
                     finish: "tool_call", in: 400, cached: 200 });
  assert.deepEqual({ model: b.model, text: b.text, in: b.tokens_in, out: b.tokens_out }, { model: "old-model", text: "Hello.", in: 10, out: 2 });
});
