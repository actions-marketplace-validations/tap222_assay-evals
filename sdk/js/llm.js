"use strict";
/**
 * Model responses read into one shape, as the Python SDK reads them (assay_sdk/llm.py): text, the
 * tool calls the model asked for, token usage, and why it stopped, in one vocabulary:
 * stop | length | tool_call | refusal | content_filter | error | null.
 */

const FINISH = {
  end_turn: "stop", stop_sequence: "stop", stop: "stop", pause_turn: "stop",
  max_tokens: "length", length: "length", model_context_window_exceeded: "length",
  tool_use: "tool_call", tool_calls: "tool_call", function_call: "tool_call", "tool-calls": "tool_call",
  refusal: "refusal", content_filter: "content_filter", "content-filter": "content_filter",
  error: "error", other: null, unknown: null,
};

const finish = (why) => (why == null ? null : why in FINISH ? FINISH[why] : String(why));

function args(value) {
  if (value == null) return {};
  if (typeof value === "string") {
    try {
      const v = JSON.parse(value);
      return v && typeof v === "object" && !Array.isArray(v) ? v : { value: v };
    } catch {
      return { raw: value };
    }
  }
  return typeof value === "object" && !Array.isArray(value) ? value : { value };
}

const num = (v) => (typeof v === "number" ? v : v && typeof v === "object" && typeof v.total === "number" ? v.total : undefined);

function anthropic(resp) {
  const blocks = Array.isArray(resp && resp.content) ? resp.content : [];
  const text = blocks.filter((b) => b && b.type === "text").map((b) => b.text || "").join("");
  const toolCalls = blocks.filter((b) => b && b.type === "tool_use")
    .map((b) => ({ name: b.name, arguments: args(b.input), id: b.id }));
  const u = (resp && resp.usage) || {};
  return { provider: "anthropic", model: resp && resp.model, text: text || undefined, toolCalls,
           finishReason: finish(resp && resp.stop_reason),
           usage: { input: u.input_tokens, output: u.output_tokens, cached: u.cache_read_input_tokens } };
}

function openai(resp) {
  const r = resp || {};
  if (Array.isArray(r.output) && !Array.isArray(r.choices)) { // the Responses API
    const items = r.output;
    const parts = items.filter((i) => i && i.type === "message").flatMap((i) => i.content || []);
    const text = r.output_text || parts.filter((c) => c && (c.type === "output_text" || c.type === "text"))
      .map((c) => c.text || "").join("");
    const toolCalls = items.filter((i) => i && i.type === "function_call")
      .map((i) => ({ name: i.name, arguments: args(i.arguments), id: i.call_id || i.id }));
    const refusal = parts.some((c) => c && c.type === "refusal");
    const incomplete = r.incomplete_details && r.incomplete_details.reason;
    const why = refusal ? "refusal" : toolCalls.length ? "tool_call" : incomplete === "max_output_tokens" ? "length"
      : incomplete === "content_filter" ? "content_filter" : r.status === "completed" ? "stop" : null;
    const u = r.usage || {};
    return { provider: "openai", model: r.model, text: text || undefined, toolCalls, finishReason: why,
             usage: { input: u.input_tokens, output: u.output_tokens,
                      cached: u.input_tokens_details && u.input_tokens_details.cached_tokens,
                      reasoning: u.output_tokens_details && u.output_tokens_details.reasoning_tokens } };
  }
  const choice = (Array.isArray(r.choices) && r.choices[0]) || {};
  const msg = choice.message || {};
  const toolCalls = (msg.tool_calls || []).map((c) => ({ name: c.function && c.function.name,
                                                          arguments: args(c.function && c.function.arguments), id: c.id }));
  const u = r.usage || {};
  return { provider: "openai", model: r.model, text: msg.content || undefined, toolCalls,
           finishReason: msg.refusal ? "refusal" : finish(choice.finish_reason),
           usage: { input: u.prompt_tokens, output: u.completion_tokens,
                    cached: u.prompt_tokens_details && u.prompt_tokens_details.cached_tokens,
                    reasoning: u.completion_tokens_details && u.completion_tokens_details.reasoning_tokens } };
}

/** A Vercel AI SDK model result (what doGenerate returns): content parts and usage, current or older. */
function aiSdk(result, modelId) {
  const r = result || {};
  const content = Array.isArray(r.content) ? r.content : [];
  const text = content.length ? content.filter((c) => c && c.type === "text").map((c) => c.text || "").join("")
    : typeof r.text === "string" ? r.text : "";
  const calls = content.length ? content.filter((c) => c && c.type === "tool-call") : r.toolCalls || [];
  const toolCalls = calls.map((c) => ({ name: c.toolName, arguments: args(c.input !== undefined ? c.input : c.args),
                                        id: c.toolCallId }));
  const u = r.usage || {};
  const why = r.finishReason && typeof r.finishReason === "object" ? r.finishReason.unified : r.finishReason;
  const cached = typeof u.inputTokens === "object" && u.inputTokens ? u.inputTokens.cacheRead : u.cachedInputTokens;
  const reasoning = typeof u.outputTokens === "object" && u.outputTokens ? u.outputTokens.reasoning : u.reasoningTokens;
  return { provider: "ai-sdk", model: (r.response && r.response.modelId) || modelId, text: text || undefined, toolCalls,
           finishReason: finish(why),
           usage: { input: num(u.inputTokens) ?? u.promptTokens, output: num(u.outputTokens) ?? u.completionTokens,
                    cached, reasoning } };
}

/** {model or model prefix: [input, output, cached?]} dollars per million tokens, from ASSAY_PRICES. */
function envPrices() {
  try {
    const p = JSON.parse(process.env.ASSAY_PRICES || "{}");
    return p && typeof p === "object" ? p : {};
  } catch {
    return {};
  }
}

/** The price for a model: its own, or the longest model prefix that has one (as in Python). */
function priceOf(prices, model) {
  if (!model) return null;
  const keys = Object.keys(prices).filter((k) => model === k || model.startsWith(k));
  return keys.length ? prices[keys.sort((a, b) => b.length - a.length)[0]] : null;
}

/** Dollars for one call, or undefined when there's no price for its model. */
function costOf(r, prices = envPrices()) {
  const p = priceOf(prices, r.model);
  const u = r.usage || {};
  if (!Array.isArray(p) || u.input == null) return undefined;
  const cached = u.cached || 0;
  const fresh = r.provider === "anthropic" ? u.input : Math.max(0, u.input - cached); // Anthropic counts them apart
  const perCached = p.length >= 3 ? p[2] : p[0]; // no cached price: the input price, an upper bound
  return (fresh * p[0] + cached * perCached + (u.output || 0) * p[1]) / 1e6;
}

module.exports = { anthropic, openai, aiSdk, finish, args, priceOf, costOf, envPrices, FINISH };
