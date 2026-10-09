"use strict";
/**
 * Model calls recorded without a run.llm() per call:
 *
 *   const client = instrument(new Anthropic());        // or new OpenAI()
 *   const model = wrapLanguageModel({ model: openai("gpt-x"), middleware: assayMiddleware() });  // Vercel AI SDK
 *
 * Inside assayCase(), every call is recorded on the case's run: model, tokens, cost (ASSAY_PRICES),
 * text, the tool definitions it was offered, the tool calls it asked for, and why it stopped.
 * Outside a case nothing is recorded, so an instrumented client can be the app's own. Streams are
 * left alone: the caller reads them, and there's nothing whole to record.
 */
const { AsyncLocalStorage } = require("async_hooks");
const llm = require("./llm");

const storage = new AsyncLocalStorage();

/** The run of the assayCase() this code runs inside, or undefined. */
function currentRun() {
  return storage.getStore();
}

function record(run, read, params, started, error) {
  try {
    const p = params || {};
    if (error) {
      run.llm({ model: p.model, finishReason: "error", tools: p.tools, started,
                error: `${error && error.name ? error.name : "Error"}: ${error && error.message ? error.message : error}`.slice(0, 2000) });
      return;
    }
    const r = read();
    if (!r.model && p.model) r.model = p.model;
    run.llm({ model: r.model, tokensIn: r.usage.input, tokensOut: r.usage.output, tokensCached: r.usage.cached,
              tokensReasoning: r.usage.reasoning, costUsd: llm.costOf(r), text: r.text, finishReason: r.finishReason,
              toolCalls: r.toolCalls.length ? r.toolCalls : undefined, tools: p.tools, started });
  } catch {
    // recording must never break the call
  }
}

/** owner[name](params) recorded on the current run. The step is written before the caller's await
 * resumes, so it lands in order; withResponse()/asResponse() on the returned promise still work. */
function wrap(owner, name, read) {
  const original = owner[name];
  if (typeof original !== "function" || original.__assay) return false;
  const patched = function (params, ...rest) {
    const run = currentRun();
    const started = new Date();
    const pending = original.call(this, params, ...rest);
    if (!run || (params && params.stream) || !pending || typeof pending.then !== "function") return pending;
    const recorded = Promise.resolve(pending).then(
      (resp) => { record(run, () => read(resp), params, started); return resp; },
      (err) => { record(run, null, params, started, err); throw err; });
    for (const extra of ["withResponse", "asResponse"]) {
      if (typeof pending[extra] === "function") recorded[extra] = pending[extra].bind(pending);
    }
    return recorded;
  };
  patched.__assay = true;
  owner[name] = patched;
  return true;
}

/**
 * Records the model calls an Anthropic or OpenAI client makes: messages.create (and beta), chat
 * completions and responses. Returns the same client; instrumenting it twice changes nothing.
 */
function instrument(client) {
  if (!client || typeof client !== "object") return client;
  const done = [];
  const at = (path) => path.reduce((o, k) => (o && o[k]), client);
  const targets = [
    [["messages"], "create", llm.anthropic, "anthropic messages.create"],
    [["beta", "messages"], "create", llm.anthropic, "anthropic beta.messages.create"],
    [["chat", "completions"], "create", llm.openai, "openai chat.completions.create"],
    [["responses"], "create", llm.openai, "openai responses.create"],
  ];
  for (const [path, name, read, label] of targets) {
    const owner = at(path);
    if (owner && wrap(owner, name, read)) done.push(label);
  }
  if (!Object.prototype.hasOwnProperty.call(client, "__assay")) {
    Object.defineProperty(client, "__assay", { value: done, enumerable: false });
  }
  return client;
}

/**
 * Vercel AI SDK middleware: wrapLanguageModel({ model, middleware: assayMiddleware() }). Records
 * each generate call (generateText, generateObject, and agents' steps) on the current run.
 */
function assayMiddleware() {
  return {
    wrapGenerate: async ({ doGenerate, params, model }) => {
      const run = currentRun();
      const started = new Date();
      const modelId = model && model.modelId;
      const p = { model: modelId, tools: params && params.tools };
      if (!run) return doGenerate();
      try {
        const result = await doGenerate();
        record(run, () => llm.aiSdk(result, modelId), p, started);
        return result;
      } catch (err) {
        record(run, null, p, started, err);
        throw err;
      }
    },
  };
}

module.exports = { instrument, assayMiddleware, currentRun, storage };
