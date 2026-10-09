[Assay](../README.md) › [Documentation](README.md)

# Any language: `assay test` with your own recorder

Assay has SDKs for Python ([sdk/python](../sdk/python/README.md)) and JavaScript and TypeScript
([sdk/js](../sdk/js/README.md)). A project in another language (Go, Java, Ruby, Rust, C#) gets the
same checks, baselines and `assay diff` from a recorder of a few dozen lines: `assay test` runs any
command, and reads what it wrote to a file.

## How `assay test` talks to your tests

`assay test` runs `[test] command` from `assay.toml` with these environment variables set:

| Variable | What it is |
|---|---|
| `ASSAY_PATH` | The file to append events to, one JSON object per line |
| `ASSAY_TEST_RUN` | This test run's id: put it in every case's `test.run` |
| `ASSAY_TEST_ATTEMPT` | The attempt, from 0, when `--repeat` runs each case more than once |
| `ASSAY_PRICES` | Optional: `{model: [input, output, cached]}` dollars per million tokens, from `[prices]` |

When the command exits, `assay test` checks every case it finds in the file, compares each with its
last passing run, and exits `0` (nothing got worse), `1` (a regression) or `6` (inconclusive).

## What to write, per test case

Each line is an event with the same envelope: `"v": 1`, a unique `"id"` (32 hex characters is
fine), `"ts"` (ISO 8601, UTC) and a `"type"`. One test case is a run:

1. **`run.start`**: `run_id` (unique per case and attempt), `kind: "agent"`, `task` (a short name),
   and `test: {"case": "<file>::<test name>", "run": ASSAY_TEST_RUN, "attempt": ASSAY_TEST_ATTEMPT}`.
   The case id is what ties a case to its baseline, so keep it stable, at most 128 characters.
2. **`step`** for each thing the AI did, numbered by `seq` from 0:
   - `kind: "llm"`: a model call, with `model`, `tokens_in`, `tokens_out`, `cost_usd`, `text`,
     `tools` (the names offered), `tool_schemas` (`{name: input schema}`), `tool_calls`
     (`[{"name", "arguments", "id"}]`), `finish_reason` (`stop`, `length`, `tool_call`, `refusal`),
     and `prompt` (`"id@version"`).
   - `kind: "tool"`: a tool call, with `name`, `args`, `result`, and `status` (`ok` or `error`, with
     `error`).
   - `kind: "approval"`: a decision to allow an action: `name` (the action), `decision`
     (`approved`, `rejected`, `pending`), `by`.
   - `kind: "answer"`: the reply, as `text`.
3. **`check`** for each thing your test checked: `test` (as above), `run_id`, `field` (its name),
   `status` (`pass` or `fail`) and `reason`. Record the test's own result as one check, so a
   failing assert fails the case.
4. **`run.end`**: `run_id`, `status` (`completed` or `failed`), and `outcome` (`resolved`,
   `unresolved` or `escalated`).

A **`prompt`** event (`prompt_id`, `version`, `template`) registers a prompt version, so a
regression shows the prompt's diff next to it. Every field and its limits are in the
[event schema](event-schema.md), and `assay schema` prints it as JSON Schema.

Append each event in one write: when tests run in parallel, they share the file.

## A recorder in Ruby

The whole recorder, and one test with it. A Go, Java or Rust one is the same shape.

```ruby
require "json"
require "securerandom"
require "time"

# Assay: one test case as a run, appended to ASSAY_PATH (nothing is written without it).
class AssayRun
  attr_reader :steps

  def initialize(case_id, task)
    @case, @task, @id, @seq, @steps = case_id, task, SecureRandom.hex(16), 0, []
    test = { case: case_id, run: ENV.fetch("ASSAY_TEST_RUN", "local") }
    test[:attempt] = ENV["ASSAY_TEST_ATTEMPT"].to_i if ENV["ASSAY_TEST_ATTEMPT"]
    @test = test
    emit(type: "run.start", run_id: @id, kind: "agent", task: task, test: @test)
  end

  def step(kind, **fields)
    @steps << { kind: kind, **fields }
    emit(type: "step", run_id: @id, seq: (@seq += 1) - 1, kind: kind, **fields)
  end

  def llm(model:, tokens_in:, tokens_out:, tools: nil)
    step("llm", model: model, tokens_in: tokens_in, tokens_out: tokens_out, tools: tools)
  end

  def tool(name, args, result)
    step("tool", name: name, args: args, result: result, status: "ok")
  end

  def approval(action, decision = "approved")
    step("approval", name: action, decision: decision)
  end

  def answer(text)
    step("answer", text: text)
  end

  def check(field, passed, reason = nil)
    emit(type: "check", test: @test, run_id: @id, field: field, status: passed ? "pass" : "fail", reason: reason)
  end

  def finish(passed, outcome = "resolved")
    emit(type: "run.end", run_id: @id, status: passed ? "completed" : "failed", outcome: outcome)
  end

  # Runs the test body as a case: records whether it passed, and re-raises a failure.
  def self.case(case_id, task)
    run = new(case_id, task)
    begin
      yield run
      run.check("test", true)
      run.finish(true)
    rescue StandardError => e
      run.check("test", false, e.message)
      run.finish(false)
      raise
    end
  end

  private

  def emit(event)
    path = ENV["ASSAY_PATH"] or return
    line = JSON.generate({ v: 1, id: SecureRandom.hex(16), ts: Time.now.utc.iso8601(6) }.merge(event).compact)
    File.open(path, "a") { |f| f.write(line + "\n") }
  end
end

# A test: a support agent that must get approval before a refund.
def support_agent(run, order_id)
  run.llm(model: "demo-model", tokens_in: 850, tokens_out: 60, tools: ["get_order", "refund"])
  order = { "O-17" => { price: 27.61 } }.fetch(order_id)
  run.tool("get_order", { order_id: order_id }, order)
  run.approval("refund") unless ENV["SKIP_APPROVAL"] == "1"
  run.tool("refund", { order_id: order_id, amount: order[:price] }, { refunded: order[:price] })
  run.answer("Refunded $#{order[:price]}.")
end

AssayRun.case("refund_test.rb::refunds a delivered order", "refund") do |run|
  support_agent(run, "O-17")
  approved = run.steps.index { |s| s[:kind] == "approval" }
  refunded = run.steps.index { |s| s[:kind] == "tool" && s[:name] == "refund" }
  run.check("approval before refund", approved && approved < refunded, "refund ran without an approval")
end
```

With `command = "ruby refund_test.rb"` in `assay.toml`, `assay test` passes and sets the baseline.
Run it again with `SKIP_APPROVAL=1` and the case regresses, and `assay diff` shows the path that
changed: `get_order → approval(refund) → refund` became `get_order → refund`.

Rules that every run must keep (`requires_approval`, `never`, the PII check) go in `assay.toml` as
for any project, and `assay test --upload` sends the run to your dashboard.
