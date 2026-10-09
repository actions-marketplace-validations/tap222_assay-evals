"use strict";
/**
 * Assay for JavaScript and TypeScript: record what your AI does in a test (each tool call, each
 * model call, the answer), so `assay test` can compare every test with its last passing run.
 *
 *   const { assayCase } = require("assay-evals");
 *   test("refunds a delivered order", () => assayCase("refund", async (run) => {
 *     run.expect().mustCall("get_order").maxSteps(6);
 *     const order = await run.call("get_order", getOrder, { orderId: "O-17" });
 *     run.llm({ model: "claude-opus-5-5", tokensIn: 850, tokensOut: 60 });
 *     run.answer(`Refunded $${order.price}.`);
 *   }));
 *
 * Run it with `assay test` (`[test] command = "npx jest test/ai"` in assay.toml). Events are written
 * to ASSAY_PATH, which `assay test` sets; with no ASSAY_PATH nothing is written, and the run is kept
 * in memory for the test's own asserts. Nothing is ever sent to a server from here: `assay test
 * --upload` sends the whole run once it's checked.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { instrument, assayMiddleware, currentRun, storage } = require("./instrument");
const { costOf } = require("./llm");

const MAX_TEXT = 20000;
const EXPECT = "assay.expect@1";

function id() {
  return crypto.randomUUID().replace(/-/g, "");
}

/** JSON-safe, with long strings cut: what's recorded never breaks a test. */
function clean(value, depth = 0) {
  if (value === undefined) return undefined;
  if (value === null || typeof value === "number" || typeof value === "boolean") return value;
  if (typeof value === "string") return value.length > MAX_TEXT ? value.slice(0, MAX_TEXT) : value;
  if (typeof value === "bigint") return value.toString();
  if (value instanceof Date) return value.toISOString();
  if (depth > 20) return "[too deep]";
  if (Array.isArray(value)) return value.map((v) => clean(v, depth + 1) ?? null);
  if (typeof value === "object") {
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      const c = clean(v, depth + 1);
      if (c !== undefined) out[k] = c;
    }
    return out;
  }
  return String(value);
}

function emit(event) {
  const file = process.env.ASSAY_PATH;
  if (!file) return;
  const line = JSON.stringify({ v: 1, id: id(), ts: new Date().toISOString(), ...event }) + "\n";
  fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
  fs.appendFileSync(file, line); // one write per event: test workers sharing the file don't split lines
}

const MAX_CASE = 128;

/** A case id the server takes: a long one keeps its start and a hash of the whole, as in Python. */
function caseIdOf(full) {
  if (full.length <= MAX_CASE) return full;
  return full.slice(0, MAX_CASE - 9) + "~" + crypto.createHash("sha1").update(full).digest("hex").slice(0, 8);
}

function testRef(caseId) {
  const ref = { case: caseId, run: process.env.ASSAY_TEST_RUN || "local" };
  const attempt = process.env.ASSAY_TEST_ATTEMPT;
  if (attempt !== undefined && attempt !== "") ref.attempt = Number(attempt);
  return ref;
}

/** A prompt template, registered by version so a regression shows the prompt change next to it.
 * Returns "id@version", for `run.llm({ prompt })`. */
function prompt(promptId, version, template) {
  emit({ type: "prompt", prompt_id: promptId, version: String(version), template: clean(template) });
  return `${promptId}@${version}`;
}

class Expectations {
  constructor(run) {
    this._run = run;
    this._rules = [];
  }
  _add(field, rule) {
    this._rules.push({ field, rule });
    return this;
  }
  _calls(tool) {
    return this._run.steps.filter((s) => s.kind === "tool" && s.name === tool);
  }
  mustCall(tool) {
    return this._add(`expect.must_call(${tool})`, () =>
      this._calls(tool).length ? null : `Expected a call to ${tool}(); it was never called.`);
  }
  mustNotCall(tool) {
    return this._add(`expect.must_not_call(${tool})`, () =>
      this._calls(tool).length ? `${tool}() was called, and must not be.` : null);
  }
  mustCallBefore(first, then) {
    return this._add(`expect.must_call_before(${first}, ${then})`, () => {
      const seq = (t) => this._calls(t).map((s) => s.seq);
      const a = seq(first), b = seq(then);
      if (!b.length) return null;
      return a.length && Math.min(...a) < Math.min(...b) ? null : `${then}() ran before ${first}().`;
    });
  }
  maxSteps(n) {
    return this._add(`expect.max_steps(${n})`, () => {
      const steps = this._run.steps.filter((s) => s.kind !== "answer").length;
      return steps <= n ? null : `${steps} steps, more than ${n}.`;
    });
  }
  mustAnswer(containing) {
    return this._add(containing ? `expect.must_answer(${containing})` : "expect.must_answer", () => {
      const text = this._run.answerText;
      if (text == null || text === "") return "There was no answer.";
      return !containing || text.includes(containing) ? null : `The answer doesn't contain ${JSON.stringify(containing)}.`;
    });
  }
  /** Records each rule as a check of the run; returns what failed. */
  verify() {
    const failed = [];
    for (const { field, rule } of this._rules) {
      const why = rule();
      this._run.check(field, !why, why || undefined, EXPECT);
      if (why) failed.push(`${field}: ${why}`);
    }
    this._rules = [];
    return failed;
  }
}

// A tool's description, kept in its schema as the Python SDK keeps it: the model reads it to choose
// a tool, so a reworded description is a change `assay diff` shows.
const DESCRIPTION = "x-assay-description";

/** {name: input schema} from tool definitions: Anthropic's input_schema, OpenAI's function
 * parameters, MCP's inputSchema. A tool's description goes in as DESCRIPTION. */
function toolSchemas(tools) {
  const out = {};
  for (const t of tools || []) {
    if (!t || typeof t !== "object") continue;
    const fn = t.function && typeof t.function === "object" ? t.function : t;
    const schema = fn.input_schema || fn.parameters || fn.inputSchema;
    if (!fn.name || !schema || typeof schema !== "object") continue;
    out[String(fn.name).slice(0, 128)] =
      typeof fn.description === "string" && fn.description ? { ...schema, [DESCRIPTION]: fn.description.slice(0, 4096) } : schema;
  }
  return out;
}

function toolNames(tools) {
  if (tools === undefined) return undefined;
  return tools.map((t) => String(typeof t === "string" ? t : (t && (t.name || (t.function && t.function.name))) || "").slice(0, 128));
}

class Run {
  constructor(task, { caseId, tags, kind = "agent" } = {}) {
    this.id = id();
    this.task = task;
    this.caseId = caseId;
    this.steps = [];
    this.answerText = undefined;
    this.outcomeValue = undefined;
    this._seq = 0;
    this._toolSchemas = {};
    this._expectations = [];
    this._ended = false;
    emit({ type: "run.start", run_id: this.id, kind, task, ...(caseId ? { test: testRef(caseId) } : {}),
           ...(tags ? { tags: clean(tags) } : {}) });
  }

  _step(kind, fields, started) {
    const step = { kind, seq: this._seq++, ...fields };
    this.steps.push(step);
    const out = {};
    for (const [k, v] of Object.entries(step)) if (v !== undefined) out[k] = clean(v);
    emit({ type: "step", run_id: this.id, ...out, ...(started instanceof Date ? { ts: started.toISOString() } : {}) });
    return step;
  }

  /** A tool call already made. */
  tool(name, args, result, { error } = {}) {
    this._step("tool", { name, args: args ?? {}, result, status: error ? "error" : "ok", error });
  }

  /** Calls fn(args), records it as a tool call (result or error) and returns its result. */
  async call(name, fn, args = {}) {
    try {
      const result = await fn(args);
      this.tool(name, args, result);
      return result;
    } catch (e) {
      this.tool(name, args, undefined, { error: `${e && e.name ? e.name : "Error"}: ${e && e.message ? e.message : e}` });
      throw e;
    }
  }

  /** A model call: { model, tokensIn, tokensOut, costUsd, prompt ("id@version"), text, finishReason, tools,
   * toolCalls, tokensCached, tokensReasoning, started, error }. tools: names, or the tool definitions
   * the model was given (Anthropic, OpenAI, or an MCP tools/list). Without costUsd, the cost comes from
   * ASSAY_PRICES ([prices] in assay.toml) when the model has a price. instrument() fills all of it. */
  llm({ model, tokensIn, tokensOut, costUsd, prompt: promptRef, text, finishReason, tools, toolCalls, tokensCached,
        tokensReasoning, started, error } = {}) {
    if (costUsd === undefined && model && tokensIn != null) {
      costUsd = costOf({ model, usage: { input: tokensIn, output: tokensOut, cached: tokensCached } });
    }
    const fresh = {};
    for (const [name, schema] of Object.entries(toolSchemas(tools))) {
      if (JSON.stringify(this._toolSchemas[name]) !== JSON.stringify(schema)) fresh[name] = schema;
    }
    Object.assign(this._toolSchemas, fresh); // sent once per run and tool, not with every call
    this._step("llm", { model, tokens_in: tokensIn, tokens_out: tokensOut, cost_usd: costUsd, prompt: promptRef,
                        text, finish_reason: finishReason, tools: toolNames(tools),
                        tool_schemas: Object.keys(fresh).length ? fresh : undefined,
                        tool_calls: toolCalls, tokens_cached: tokensCached, tokens_reasoning: tokensReasoning,
                        status: error ? "error" : "ok", error }, started);
  }

  answer(text) {
    this.answerText = text;
    this._step("answer", { text });
  }

  /** "resolved", "unresolved" or "escalated". */
  outcome(value) {
    this.outcomeValue = value;
  }

  /** A check of the run's own: a domain rule, or a test's assert. */
  check(field, passed, reason, evaluator) {
    emit({ type: "check", ...(this.caseId ? { test: testRef(this.caseId) } : {}), status: passed ? "pass" : "fail",
           run_id: this.id, field, ...(evaluator ? { evaluator } : {}), ...(reason ? { reason: clean(reason) } : {}) });
  }

  /** What the run must do, checked when the case ends: run.expect().mustCall("get_order").maxSteps(6). */
  expect() {
    const e = new Expectations(this);
    this._expectations.push(e);
    return e;
  }

  end(status = "completed", error) {
    if (this._ended) return;
    this._ended = true;
    emit({ type: "run.end", run_id: this.id, status, ...(this.outcomeValue ? { outcome: this.outcomeValue } : {}),
           ...(error ? { error: clean(error) } : {}) });
  }
}

/** A run outside a test case, e.g. recorded by the app itself. */
function startRun(task, options = {}) {
  return new Run(task, options);
}

/** The current test's id, "<file>::<name>", from Jest's or Vitest's expect.getState(). */
function currentTest() {
  const state = typeof globalThis.expect === "function" && typeof globalThis.expect.getState === "function"
    ? globalThis.expect.getState() : null;
  if (!state || !state.currentTestName) return null;
  const file = state.testPath ? path.relative(process.cwd(), state.testPath).split(path.sep).join("/") : "";
  return { id: file ? `${file}::${state.currentTestName}` : state.currentTestName, name: state.currentTestName };
}

/**
 * One test as an Assay case. Opens a run named after the current test, gives it to fn, then
 * checks the run's expectations, records whether the test passed, and ends the run. Throws when
 * fn throws or an expectation fails, so the test fails as it should.
 */
async function assayCase(name, fn, { tags } = {}) {
  if (typeof name === "function") [fn, name] = [name, undefined];
  const current = currentTest();
  const caseId = current ? caseIdOf(current.id) : name && caseIdOf(name);
  if (!caseId) throw new Error("assayCase needs a name outside Jest or Vitest: assayCase(\"refund\", fn)");
  const run = new Run(name || current.name, { caseId, tags });
  let result, thrown;
  try {
    result = await storage.run(run, () => fn(run)); // instrument()ed clients record on this run
  } catch (e) {
    thrown = e;
  }
  const failed = run._expectations.flatMap((e) => e.verify());
  const reason = thrown ? String(thrown && thrown.message ? thrown.message : thrown).slice(0, 2000) : undefined;
  run.check("test", !thrown, reason); // the test's own asserts; expectations are recorded as themselves
  run.end(thrown ? "failed" : "completed", reason);
  if (thrown) throw thrown;
  if (failed.length) throw new Error("The run failed Assay's checks:\n  " + failed.join("\n  "));
  return result;
}

module.exports = { assayCase, startRun, prompt, caseIdOf, toolSchemas, instrument, assayMiddleware, currentRun,
                   Run, Expectations };
