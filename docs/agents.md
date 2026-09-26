[Assay](../README.md) › [Documentation](README.md)

# Agents

## Agents: evaluating the trajectory, not just the answer

An agent reasons, calls tools, reads what they return, changes things, and answers. Judging
only the answer misses a refund issued to the wrong order, or a correct answer reached by
deleting something first. Send each run as a **trajectory**:

```json
{"trajectory_id": "agent-0923.case-017.a0", "run_id": "agent-0923", "case_id": "case-017", "attempt": 0,
 "task": "refund_request", "started_at": "2026-09-23T10:00:00Z", "answer": "Refunded $27.61.",
 "lineage": {"prompt": "support_agent@v5", "model": "claude-sonnet-5"},
 "steps": [
   {"kind": "reason", "model": "claude-sonnet-5", "tokens": 812, "cost_usd": 0.0024},
   {"kind": "tool", "name": "get_order", "args": {"order_id": "O-10017"}, "result": {"price": 27.61}},
   {"kind": "tool", "name": "issue_refund", "args": {"order_id": "O-10017", "amount": 27.61}},
   {"kind": "state", "name": "refund:O-10017", "args": {"op": "create"}, "result": {"amount": 27.61}},
   {"kind": "answer", "text": "Refunded $27.61."}]}
```

Also send what each test case expects to `POST /v1/agents/references`:
- the tool calls, in order where it matters. Arguments match partially, and a call can be
  `optional` or `any_order`;
- tools that may be called beyond those (`allow_extra`, e.g. read-only lookups);
- the answer;
- end-state assertions, such as `{"object": "refund:O-10017", "exists": true}` or
  `{"object": "order:*", "field": "qty", "equals": 3}`;
- a step budget.

OpenTelemetry works too. `execute_tool` spans become tool steps and model spans become
reasoning steps (see the mapping at `/docs`).

`POST /v1/agents/runs/{run}/evaluate` checks every trajectory five ways and stores the checks
as evaluation results. Failure causes, flakiness across attempts and the release call then
work on agents unchanged.

| Check | Passes when |
|---|---|
| answer | the final answer has the expected value (whole-word, or written another usual way) |
| end state | the world afterwards matches, folded from the state-change steps |
| tool calls | every required call was made with the right arguments, in order. Retrying a call that errored is fine; so are allowed extras |
| safety | no critical path contract broke. Contracts see tool arguments: `delete_order` never runs `where` `confirmed` isn't true; `issue_refund` only after `get_order` with the `same` `order_id`; at most 2 `identical` `lookup_customer` calls |
| efficiency | within the step budget, and no call repeated 3 times with identical arguments |

For a failing run, **credit assignment** finds the first bad step and how it went wrong:
unsafe action, a tool error it never recovered from (infrastructure when the error says the
tool was unavailable), a loop, the wrong tool, the right tool with the wrong arguments,
stopping before an expected call, ignoring a tool result that held the answer, the wrong end
state, or a wrong answer after correct calls. These are the mechanisms Failure causes groups
by, so you get lines like "Wrong tool: `search_orders` where `get_order` was expected: 21
cases, a regression since `support_agent` v4 → v5".

Tool calls are also recorded as pipeline steps. So the workflow graph draws the agent's tool
graph, path contracts and shifts apply to tool sequences, Trace shows any run step by step
against its reference, and the measures count tool failures and model cost. A document
pipeline is just an agent with a fixed path.

### When a run is evaluated: when it ends, not on a timer

An agent can take ten seconds or five minutes, so Assay doesn't evaluate after a fixed
delay. Each run goes through a lifecycle:

- **running:** `run.start` (or its first step) has arrived, and `run.end` hasn't. It isn't
  judged half-way.
- **ended:** `run.end` says `completed` or `failed`. A run with no events for 30 minutes
  (`ASSAY_ABANDON_MINUTES`) is marked **abandoned**: its process died. An agent that's
  quiet for longer between steps can be given its own limit, by its runs' `task`:

  ```bash
  curl -X PUT $ASSAY_URL/v1/agents/limits -H "Authorization: Bearer $ASSAY_KEY" \
    -d '{"abandon_minutes": {"deep_research": 180, "*": 10}}'   # "*": this source's other agents; null removes one
  ```
- **evaluated:** in the same request that ended it, once its child runs (`parent_run_id`)
  have ended too.
- **evaluated again:** if more events arrive after that, e.g. a late step.

With OpenTelemetry, a run ends when its root span arrives. Spans are exported as they end,
often over several batches, so the run's steps are added up across batches, and a tool span
that arrives before the root span doesn't end the run early.

Every run gets the checks that need no expectations: it **finished**, it kept the critical
path contracts, it didn't **loop**, and no **tool error** went unrecovered. A test-case run
also gets its case's checks, so an evaluation run fills in as its cases finish. A background
sweep, every 60 seconds (`ASSAY_EVALUATE_SECONDS`) or on each `/v1/cron` call on Vercel,
marks quiet runs abandoned and evaluates anything left over.

| Endpoint | Returns |
|---|---|
| `GET /v1/agents/lifecycle?source=…` | how many runs are running, awaiting evaluation, evaluated, abandoned or failing, and the latest failures |
| `GET /v1/agents/limits?source=…`, `PUT /v1/agents/limits` | each agent's abandon limit, in minutes |
| `POST /v1/events/trajectories` | ingest trajectories (steps inline) |
| `POST /v1/agents/references`, `GET /v1/agents/references?source=…` | what cases expect |
| `GET /v1/agents/runs?source=…` | agent runs, newest first |
| `POST /v1/agents/runs/{run}/evaluate?source=…` | run the five checks, stored as eval results (runs still going are skipped and counted) |
| `GET /v1/agents/runs/{run}?source=…` | pass rate per check, tool precision and recall, first bad steps, and efficiency vs the baseline |
| `GET /v1/agents/trajectories/{id}?source=…` | one run step by step: divergence, end state, contract breaks, cost, and its latest evaluation |

### Plan adherence: did it do what it said it would?

An agent that plans before it acts can record the plan, and the run is checked against it.
No test case or reference is needed:

```python
run.plan(["search_customer", "get_order", {"tool": "refund", "args": {"id": "O-17"}}],
         text="Find the customer, check the order, refund it")
```

The `plan` check fails when the agent:
- skipped a planned call (or it errored and was never made);
- made planned calls out of order;
- made a planned call with other arguments than it planned.

It says where: "Strayed from its plan: called get_order (step 2) after refund, though the plan
put it first." Calls the plan didn't mention are allowed, and so is retrying a call that
errored. Replanning is allowed too: a new plan step replaces the rest of the one before, so
switching from "refund" to "escalate" isn't counted as skipping the refund. A run that failed
or stopped early is reported by **finished**, not here as well. In `pytest --assay` and
`assay test` the check is reported as "Plan adherence", under Planning. Whether the plan
itself was a good one needs a judge; this checks only that it was followed.

### An LLM judge: was the plan a good one, and does the run hang together?

Rules can check that a plan was followed. They can't check whether it was worth following,
or whether the answer agrees with what the tools returned. An LLM judge scores both, 1 to 5,
with a reason that names the step it rests on:

| Check | What the judge looks at |
|---|---|
| **Plan quality** | whether the plan, given the request and the tools offered, addresses what was asked, in a workable order, without steps it didn't need. Only for runs that record a plan |
| **Consistency** | whether the reasoning, the tool results and the answer agree: nothing contradicted, nothing stated as fact that no step established |

```bash
pip install anthropic                       # and ANTHROPIC_API_KEY, or `ant auth login`
assay test --judge                          # or: pytest --assay --assay-judge
curl -X POST "$ASSAY_URL/v1/agents/runs/nightly-0924/judge?source=events:acme" -H "Authorization: Bearer $ASSAY_KEY"
```

It costs a model call per run, so it runs only when asked. `[judge] enabled = true` in
`assay.toml` turns it on for every run. The model is `claude-opus-5` by default. `[judge] provider` and `model` (or `ASSAY_JUDGE_PROVIDER`
and `ASSAY_JUDGE_MODEL` on the server) judge with OpenAI, Gemini, Ollama or any OpenAI-compatible
server instead. A score of 3 or more passes.

The judge's results are ordinary evaluation results (evaluator `assay.judge@1`). So:
- a case whose consistency drops from its baseline is a regression, and a judge that disagrees
  with itself across attempts is flaky;
- what the judge was given is recorded, and checked against the trace like any evaluator's;
- a judge that couldn't judge isn't a failure, and never a score of 0. Its verdict is checked
  against its schema. One that isn't a verdict (unparseable, a field missing, a score that
  isn't a whole number from 1 to 5, an answer cut off) is asked for again, then recorded as
  `INVALID` with what the judge actually said. A rate limit is `RATE_LIMITED`, a timeout
  `TIMEOUT`, a 5xx or connection error `INFRA_ERROR`, a refusal or a rejected request
  `EVALUATOR_ERROR`. Each result records how many tries it took.

A test run's runs are judged several at once, within limits, and the run says what judging cost:

```
Judged 40 runs with claude-opus-5 (plan quality, consistency).
43 LLM calls, 3 retries, 1,204,300 tokens in, 18,120 out, estimated $6.47.
```

```toml
[judge]
concurrency = 4     # runs judged at once
rate_limit = 50     # model calls a minute, shared by all of them; a 429 pauses them all (Retry-After)
timeout = 120       # seconds a call may take
retries = 3         # after a 429, a 5xx, a timeout, or a verdict that isn't valid
max_time = 600      # seconds for all the judging
budget_usd = 5      # dollars for all the judging

[judge.prices]      # dollars per million tokens: input, output[, cached]
"claude-opus-5" = [5, 25]
```

There's one retry layer: the provider SDK's own retries are off while judging, so every
request is counted once, with its tokens. When `max_time` or `budget_usd` is reached, the runs
left aren't judged, and they aren't failures. The cost is an estimate from `[judge.prices]`
(else `[prices]`, or `ASSAY_PRICES` as JSON); with no price it says so rather than guessing. On the server,
`ASSAY_JUDGE_CONCURRENCY`, `ASSAY_JUDGE_RATE_LIMIT`, `ASSAY_JUDGE_MAX_TIME` and
`ASSAY_JUDGE_BUDGET_USD` do the same, and the endpoint's answer has the same `summary`. This is
`assay_sdk.EvalRuntime`, which you can run your own evaluators with too
([SDK guide](../sdk/python/README.md#many-samples-at-once-evalruntime)).

**What kind of failure.** A failing score names its kind, and the kinds keep hallucinations
apart, since each needs another fix:

| Kind | The answer… |
|---|---|
| `fabricated` | states a fact no step or source contains |
| `contradicts_source` | says the opposite of a tool result or a source it was given |
| `unsupported_inference` | draws a conclusion the steps it cites don't support |
| `contradicts_itself` | says in one part what another part denies |
| `incomplete`, `policy_refusal`, `unworkable`, `inefficient`, `other` | the rest |

The report counts failures by kind, next to the baseline's (`Failures by kind: 2 contradicts
source (0 before) · 1 fabricated (0 before)`), and so do `assay diff` and the PR comment. Your own
evaluators name kinds the same way (`category` in a verdict, or `run.check(..., category=)`).

**The judge shows its evidence.** A contradiction inside a long answer (paragraph 3 against
paragraph 7) is listed as two exact quotes from the answer. Assay checks the quotes itself, word
for word. A contradiction whose quotes aren't in the answer is left out, and a verdict whose only
evidence is made up is asked for again, then recorded as `INVALID`, never as a score. The same
goes for a reason that cites a step the trace doesn't have. The judge's claim has to be backed by
the text, not taken on trust.

Requests the model declines are retried on another model server-side (`fallbacks: "default"`).
The trace is shown to the judge as data, marked as such, so instructions inside a tool result
don't steer the score. Long tool results are cut, and the cut is marked.

### Conversations and MCP

A chat is several runs, one per turn. `assay.run(..., conversation="chat-1", turn=2)` (or
`conversation_id` and `turn` on `run.start`, or `gen_ai.conversation.id` with OpenTelemetry)
links them. `GET /v1/agents/conversations?source=…` lists conversations, and
`GET /v1/agents/conversations/{id}?source=…` shows one turn by turn: what each turn was asked
and answered, its steps, and its evaluation. A case saved from a later turn keeps the turns
before it in full (`trajectory.conversation`), so it can be replayed with what was said before.

MCP steps are recorded as what they are, not as tool calls:

```python
run.tool("refund", {"id": "O-17"}, {"ok": True}, server="shop")              # an MCP tool
run.resource("file:///policies/refunds.md", policy_text, server="docs")     # a resource read
run.mcp_prompt("refund_policy_check", {"order": "O-17"}, messages, server="docs")
```

A resource's contents count as what the agent retrieved, so a judge given that policy as its
context passes the context check.

## Beyond the final answer: how the agent behaved

"Agent score: 0.87" doesn't say what changed. Assay checks what the agent did, and compares how it
behaved with each test's last passing run.

```python
from assay_sdk.testing import expect

def test_refund(assay_case):
    expect(assay_case).must_call("get_order").must_not_call("delete_order").max_steps(8) \
        .must_get_approval_before("refund").max_cost(0.05).max_latency(8) \
        .max_tools_exposed(10).max_context_tokens(8000).must_resolve()
    my_agent("Refund O-17", run=assay_case)
```

Expectations can be declared before the agent runs. They're checked when the test ends, and every
one that fails is reported together. Each is also recorded as a check (`expect.must_call(get_order)`),
so it's compared with its baseline like any other. Outside pytest, call `.verify()` or use
`with expect(run):`.

For that, the SDK records three things beyond tool calls and the answer:

| Call | Records |
|---|---|
| `run.llm(..., tokens_in=, tools=[...])` | the tools the model was offered (names, or the definitions you passed it), and how big its input was |
| `run.approval("refund", "approved" \| "rejected" \| "pending", by="manager", reason=)` | a decision to allow an action |
| `run.outcome("resolved" \| "unresolved" \| "escalated")` | whether the run did what was asked |

`pytest --assay` and `assay test` then compare each case's behavior with its baseline, and a
case fails when it got worse:

```
⚠ 3 cases behaved worse than their baseline
  tests/test_behavior.py::test_hard_case
    Cost: $0.0040 → $0.0120 (3.0×)
    Context: 1,200 tokens → 9,000 tokens (7.5×)
    Tools exposed: 8 tools → 30 tools (3.8×)
    Outcome: resolved → unresolved
  tests/test_behavior.py::test_policy_edge
    Approval for refund: approved → rejected
```

A number counts as worse when it grew 1.5× and by at least a minimum ($0.001, 1 s, 2 steps, 500
tokens, 2 tools), so small moves aren't news. An outcome counts as worse when a resolved case
stops resolving, and an approval when its decision changes. `[behavior]` in `assay.toml` sets
the ratios (0 turns one off), and `fail = false` only reports it. With repeats, a case's number
is the median of its attempts. The `requires_approval` contract puts an approval rule in
`assay.toml` instead of in every test.

### Retrieved context: what RAG puts in the prompt, and what it costs

Adding retrieval to an app often multiplies its bill. Every fragment retrieved is added to the
prompt, and each query is only a few hundred tokens bigger. Nothing looks wrong on its own until
the invoice arrives. Assay records what retrieval put into the prompt and checks it three ways.

```python
run.retrieve(question, docs, used=4)   # the fragments found; the top 4 went into the prompt
```

`fragments` are text, dicts (`text`, `id`, `tokens`, `score`, `source`), LangChain Documents or
LlamaIndex nodes. `used` says which went into the prompt: all of them (the default), the first n,
or ids. A fragment without `tokens` is estimated at four characters a token. With
`assay.instrument()` on, LangChain and LlamaIndex retrievers are recorded without this call
(what the outermost retriever returned).

**Per query, against the baseline.** A case whose queries put more fragments or more tokens into
the prompt (1.5× and at least 2 fragments or 200 tokens) behaved worse. The line names the
retriever and the share of the prompt it took:

```
  tests/test_support.py::test_refund_policy
    Retrieved context (kb_search): 3 fragments, 420 tokens → 12 fragments, 2,900 tokens (68% of the prompt)
    Context: 1,300 tokens → 4,260 tokens (3.3×)
```

**The whole run, against the baseline.** Each case can stay under its own ratio while the
whole run triples. So the run's totals over the cases both runs have (input tokens, cost,
retrieved tokens) are compared too, at `suite = 1.25` (and at least 1,000 tokens, or a cent):

```
⚠ The whole run grew against its baseline
  Input tokens for the whole run: 41,000 → 118,000 (2.9×), over the 40 cases in both runs
    most: test_refund_policy (+6,100), test_warranty (+5,800), test_returns (+5,200)
```

**Limits, whatever the baseline.** A query over a limit fails its case, from the first run:

```toml
[behavior]
max_fragments = 8           # fragments one query puts into the prompt
max_retrieved_tokens = 3000 # tokens of fragments one query puts into the prompt
max_context_tokens = 8000   # input one model call gets
suite = 1.25                # the whole run's totals (0 turns it off)

[prices]                    # dollars per million tokens: recorded calls get their cost
"claude-opus-5" = [5, 25]
```

With `[prices]` (or `ASSAY_PRICES`), model calls recorded without a cost get one from their
tokens, so the cost checks have numbers to compare. On a pull request, the limits and `suite`
are held to the base branch's, like every other check: raising a limit is reported as
loosening it.
