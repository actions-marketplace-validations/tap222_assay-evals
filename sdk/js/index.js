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
 * to ASSAY_PATH, which `assay test` sets; `assay test --upload` sends the whole run once it's checked.
 *
 * In the app itself, with ASSAY_URL set (and ASSAY_PATH not), runs go to the server: track() opens
 * one per request, and an instrument()ed client records its model calls on it.
 *
 *   const reply = await track("whatsapp_reply", () => receptionist.reply(message), { conversationId });
 *
 * With neither set, nothing is written or sent, and the run is kept in memory.
 */

const fs = require("fs");
const path = require("path");
const crypto = require("crypto");
const { instrument, assayMiddleware, currentRun, storage, setPlainRun, plainRun } = require("./instrument");
const { summarize: summarizeSql } = require("./sql");
const { Tracker } = require("./writes");
const { costOf } = require("./llm");
const { sender } = require("./send");

const MAX_TEXT = 20000;
const DB_READS = "DB reads"; // one step per run: reads per table, left out of the flow
const DB_UNWRITTEN = "DB unwritten"; // one step per run: sent values no query used (writes.js)
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
  const send = file ? null : sender();
  if (!file && !send) return;
  const full = { v: 1, id: id(), ts: new Date().toISOString(), ...event };
  if (send) return send.add(full);
  fs.mkdirSync(path.dirname(path.resolve(file)), { recursive: true });
  fs.appendFileSync(file, JSON.stringify(full) + "\n"); // one write per event: test workers sharing the file don't split lines
}

/** Sends what's queued now (with ASSAY_URL); resolves true once nothing is left. For shutdown hooks. */
function flush() {
  const send = sender();
  return send ? send.flush() : Promise.resolve(true);
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
  mustGetApprovalBefore(action) {
    return this._add(`expect.must_get_approval_before(${action})`, () => {
      const first = this._calls(action).map((s) => s.seq);
      if (!first.length) return null;
      const ok = this._run.steps.some((s) => s.kind === "approval" && s.name === action && s.decision === "approved"
                                            && s.seq < Math.min(...first));
      return ok ? null : `${action} ran at step ${Math.min(...first)} without an approval.`;
    });
  }
  maxSteps(n) {
    return this._add(`expect.max_steps(${n})`, () => { // every step, the answer too, as in Python
      const steps = this._run.steps.length;
      return steps <= n ? null : `Took ${steps} steps; at most ${n} expected.`;
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
  constructor(task, { caseId, tags, kind = "agent", input, version, segment, conversationId, turn, user } = {}) {
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
           ...(tags ? { tags: clean(tags) } : {}), ...(input !== undefined ? { input: clean(input) } : {}),
           ...(version ? { version: clean(version) } : {}), ...(segment ? { segment } : {}),
           ...(conversationId ? { conversation_id: String(conversationId).slice(0, 128) } : {}),
           ...(turn != null ? { turn } : {}), ...(user ? { user: String(user).slice(0, 128) } : {}) });
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

  /** A decision to allow an action: approval("refund", "approved", { by: "manager" }). decision:
   * approved, rejected or pending. Contracts (requires_approval) and mustGetApprovalBefore() check it. */
  approval(action, decision = "approved", { by, reason } = {}) {
    this._step("approval", { name: action, decision, by, text: reason });
  }

  answer(text) {
    this.answerText = text;
    this._step("answer", { text });
  }

  /** A database write, from summarize() in sql.js: recorded as the step "DB INSERT bookings" with the
   * columns it set, the ones it left empty and those set to a placeholder ("Unknown"), never the values.
   * fixture: the test wrote it itself, not the app. */
  dbWrite({ op, table, set, empty, placeholder, fixture }, error) {
    this.tool(`DB ${op} ${table}`.slice(0, 128), { set, empty, placeholder, fixture: fixture || undefined }, undefined,
              { error: error ? `${error.code ? `${error.code} ` : ""}${error.message || error}`.slice(0, 500) : undefined });
  }

  /** A database read: only counted per table, and recorded once as "DB reads" when the run ends. */
  dbRead(table) {
    this._reads = this._reads || {};
    this._reads[table] = (this._reads[table] || 0) + 1;
  }

  /** A query's values, kept in memory to check against what the test sent (writes.js). */
  dbUsed(summary, values) {
    if (this._sent) this._sent.query(summary, values);
  }

  /** What the test gave the code under test: a request body, a service's input. When the run ends,
   * a value in it that no later query used is recorded by name (never by value) as "DB unwritten". */
  sent(input, label = "input") {
    try {
      this._sent = this._sent || new Tracker();
      this._sent.send(input, String(label).slice(0, 128));
    } catch {
      // recording must never break the test
    }
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
    if (this._reads) this.tool(DB_READS, this._reads, undefined);
    const unwritten = this._sent && this._sent.unwritten();
    if (unwritten) this.tool(DB_UNWRITTEN, unwritten, undefined);
    emit({ type: "run.end", run_id: this.id, status, ...(this.outcomeValue ? { outcome: this.outcomeValue } : {}),
           ...(error ? { error: clean(error) } : {}) });
  }
}

/** A run outside a test case, e.g. recorded by the app itself. */
function startRun(task, options = {}) {
  return new Run(task, options);
}

/**
 * One request of the app as a run: opens it, calls fn(run) with it current (so an instrument()ed
 * client records its model calls on it), and ends it, failed if fn throws. Returns fn's result,
 * and rethrows what it throws. Recording never breaks fn.
 */
async function track(task, fn, options = {}) {
  let run;
  try {
    run = new Run(task, options);
  } catch {
    return fn(undefined);
  }
  try {
    const result = await storage.run(run, () => fn(run));
    run.end("completed");
    return result;
  } catch (e) {
    run.end("failed", `${e && e.name ? e.name : "Error"}: ${e && e.message ? e.message : e}`.slice(0, 2000));
    throw e;
  }
}

/** The current test's id, "<file>::<name>", from Jest's or Vitest's expect.getState() (the global
 * one, or a test context's own). */
function currentTest(expectFn = globalThis.expect) {
  const state = expectFn && typeof expectFn.getState === "function" ? expectFn.getState() : null;
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
  if (typeof name === "function" && fn === undefined) [fn, name] = [name, undefined]; // assayCase(fn)
  let task, contextExpect;
  const context = name && (typeof name === "object" || typeof name === "function"); // Vitest's context is a function
  if (context && name.expect && typeof name.expect.getState === "function") {
    contextExpect = name.expect; // Vitest's context, without globals: test("…", (ctx) => assayCase(ctx, fn))
    name = undefined;
  } else if (context && typeof name.name === "string") { // node:test's context: test("…", (t) => assayCase(t, fn))
    const file = process.argv[1] ? path.relative(process.cwd(), process.argv[1]).split(path.sep).join("/") : "";
    task = name.name;
    const title = name.fullName || name.name;
    name = file ? `${file}::${title}` : title;
  }
  const current = name ? null : currentTest(contextExpect);
  const caseId = current ? caseIdOf(current.id) : name && caseIdOf(name);
  if (!caseId) throw new Error("assayCase needs a name outside Jest or Vitest: assayCase(\"refund\", fn), or with " +
                               "node:test the test's context: test(\"refund\", (t) => assayCase(t, fn))");
  const run = new Run(task || name || current.name, { caseId, tags });
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

/** What the test gave the code under test, e.g. a service's input: assay.sent(dto). Inside a recorded
 * test (assay-evals/jest, or an assayCase), a value in it that no later query used is reported by
 * name; elsewhere it does nothing. A JSON request to the app in the same process needs no call. */
function sent(input, label) {
  const run = currentRun() || plainRun();
  if (run) run.sent(input, label);
}

module.exports = { assayCase, sent, startRun, track, flush, prompt, caseIdOf, toolSchemas, instrument, assayMiddleware,
                   currentRun, Run, Expectations, currentTest, setPlainRun, plainRun, summarizeSql,
                   recordPg: (...a) => require("./pg").recordPg(...a) };
