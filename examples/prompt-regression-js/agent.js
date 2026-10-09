/**
 * A support agent with a stand-in for the model, so the demo needs no API key.
 *
 * The stand-in follows its prompt the way a real model would follow these lines: it looks up the
 * order, asks for approval before a refund, and skips the approval when the prompt tells it to
 * refund right away for an upset customer. Swap `fakeModel` for your real model call (and
 * instrument(client) records it for you).
 */
const fs = require("fs");
const path = require("path");
const { prompt } = require("assay-evals");

const ORDERS = { "O-17": { price: 27.61, status: "delivered" }, "O-18": { price: 12.0, status: "shipped" } };

/** The prompt version to use: ASSAY_DEMO_PROMPT, 1 by default. Registered so a regression shows
 * what changed in the prompt next to it. */
function loadPrompt() {
  const version = process.env.ASSAY_DEMO_PROMPT || "1";
  const text = fs.readFileSync(path.join(__dirname, "prompts", `support-${version}.txt`), "utf8");
  return { ref: prompt("support", version, text), text };
}

/** What the model decides to do. Replace with your real model call. */
function fakeModel(promptText, message) {
  const upset = ["now", "angry", "ridiculous"].some((w) => message.toLowerCase().includes(w));
  return { skipApproval: upset && promptText.toLowerCase().includes("refund right away") };
}

async function supportAgent(run, message, orderId) {
  const { ref, text } = loadPrompt();
  const decision = fakeModel(text, message);
  run.llm({ model: "demo-model", prompt: ref, tokensIn: 850, tokensOut: 60, costUsd: 0.0021, tools: ["get_order", "refund"] });
  const order = await run.call("get_order", ({ orderId: id }) => ORDERS[id], { orderId });
  let reply;
  if (order.status !== "delivered") {
    reply = `Order ${orderId} hasn't arrived yet, so it can't be refunded.`;
  } else {
    if (!decision.skipApproval) run.approval("refund", "approved", { by: "policy:under-50" });
    await run.call("refund", ({ amount }) => ({ refunded: amount }), { orderId, amount: order.price });
    reply = `Refunded $${order.price.toFixed(2)}.`;
  }
  run.answer(reply);
  run.outcome("resolved");
  return reply;
}

module.exports = { supportAgent };
