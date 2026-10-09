# assay-evals for JavaScript and TypeScript

Record what your AI does in a test (each tool call, each model call, the answer), so
[Assay](https://github.com/tap222/assay-evals) can compare every test with its last passing run.
A prompt change that makes the agent skip a step fails the test, even when the answer still looks
right, and `assay diff` shows the path before and after.

Works with Jest, Vitest and plain Node 18+. No dependencies.

## Install

```bash
npm install --save-dev assay-evals
pip install assay-server          # or: pipx install assay-server (the `assay` command)
```

`assay init` in a project with a `package.json` writes `assay.toml` and an example test for the
runner it finds (Vitest, Jest, or Node's own `node:test`), in TypeScript when the project uses it.

## A test

```ts
import { assayCase, prompt } from "assay-evals";

const SYSTEM = "You are a support agent. Refunds need approval first.";

test("refunds a delivered order", () =>
  assayCase(async (run) => {
    // What the run must do, checked when the test ends.
    run.expect().mustCall("get_order").mustCallBefore("approval", "refund").maxSteps(8);

    const order = await run.call("get_order", getOrder, { orderId: "O-17" });
    const reply = await model.messages.create({ system: SYSTEM, /* ... */ });
    run.llm({
      model: reply.model,
      tokensIn: reply.usage.input_tokens,
      tokensOut: reply.usage.output_tokens,
      prompt: prompt("support", "v2", SYSTEM),  // a prompt change shows next to a regression
    });
    run.tool("approval", { action: "refund" }, { decision: "approved" });
    await run.call("refund", refund, { orderId: "O-17", amount: order.price });
    run.answer(`Refunded $${order.price}.`);
    run.outcome("resolved");

    expect(run.steps.filter((s) => s.kind === "tool")).toHaveLength(3);  // your own asserts too
  }));
```

`assayCase` names the case after the current test (`<file>::<test name>`), checks the run's
expectations, records whether the test passed, and fails the test when an expectation fails.
With `node:test`, or Vitest without `globals: true`, pass the test's context so it can name the case:

```ts
test("refunds a delivered order", (t) => assayCase(t, async (run) => { /* ... */ }));
```

## Record model calls without a line per call

Instrument the client once, and every call made inside an `assayCase` is recorded on its run: the
model, tokens (and cached and reasoning tokens), cost, text, the tool definitions it was offered,
the tool calls it asked for, and why it stopped.

```ts
import Anthropic from "@anthropic-ai/sdk";      // or OpenAI: new OpenAI()
import { assayCase, instrument } from "assay-evals";

const client = instrument(new Anthropic());     // the app's own client: outside a case, nothing is recorded

test("answers from the clinic info", () =>
  assayCase(async (run) => {
    run.expect().maxSteps(4).mustAnswer("500");
    const reply = await receptionist(client, "How much is a consultation?");
    run.answer(reply);
  }));
```

`instrument()` covers Anthropic's `messages.create` (and `beta.messages.create`) and OpenAI's
`chat.completions.create` and `responses.create`. For the Vercel AI SDK, wrap the model:

```ts
import { wrapLanguageModel } from "ai";
import { assayMiddleware } from "assay-evals";

const model = wrapLanguageModel({ model: openai("gpt-x"), middleware: assayMiddleware() });
```

Streamed calls aren't recorded: the caller reads the stream, so there's nothing whole to record.
Costs come from the response's tokens and `[prices]` in `assay.toml`, which `assay test` passes on.

## Run it

`assay.toml` in the project:

```toml
[test]
command = "npx jest test/ai"
repeat = 1   # 3 or more tells a flaky case from a broken one
```

```bash
assay test          # runs the command, checks every case, compares it with its last passing run
assay diff          # what changed, case by case, and what changed next to it
assay test --upload # also send the run to your Assay dashboard (ASSAY_URL)
```

The first passing run becomes each case's baseline. Plain `npx jest` runs the same tests with
nothing recorded: events are written only to `ASSAY_PATH`, which `assay test` sets.

## API

| | |
|---|---|
| `assayCase([name], fn)` | One test as an Assay case; `fn(run)` records it |
| `instrument(client)`, `assayMiddleware()` | Record an Anthropic or OpenAI client's calls, or a Vercel AI SDK model's |
| `run.call(name, fn, args)` | Calls `fn(args)`, records it as a tool call (result or error), returns the result |
| `run.tool(name, args, result, { error })` | A tool call already made |
| `run.approval(action, decision, { by })` | A decision to allow an action (`requires_approval` contracts check it) |
| `run.llm({ model, tokensIn, tokensOut, costUsd, prompt, text, finishReason, tools })` | A model call. `tools`: names, or the definitions you gave the model (Anthropic, OpenAI, or an MCP `tools/list`), so a changed description or schema shows in `assay diff` |
| `run.answer(text)`, `run.outcome("resolved")` | The reply, and whether it resolved the request |
| `run.check(field, passed, reason)` | A check of your own |
| `run.expect()` | `.mustCall(t)`, `.mustNotCall(t)`, `.mustCallBefore(a, b)`, `.mustGetApprovalBefore(action)`, `.maxSteps(n)` (every step, the answer too), `.mustAnswer(text)` |
| `prompt(id, version, template)` | Registers a prompt version; returns `"id@version"` for `run.llm` |
| `run.steps` | What was recorded, for your own asserts |

Safety rules for every run (`never`, `before`, `requires_approval`, ...) and the PII check go in
`assay.toml`, as for Python: see the [Assay docs](https://github.com/tap222/assay-evals/tree/main/docs).
