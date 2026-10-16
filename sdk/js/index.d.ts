/** Assay for JavaScript and TypeScript. See README.md. */

export type Scalar = string | number | boolean | null;

export interface Step {
  kind: "tool" | "llm" | "answer" | string;
  seq: number;
  name?: string;
  args?: Record<string, unknown>;
  result?: unknown;
  status?: "ok" | "error";
  error?: string;
  model?: string;
  tokens_in?: number;
  tokens_out?: number;
  cost_usd?: number;
  prompt?: string;
  text?: string;
  finish_reason?: string;
  tools?: string[];
}

export interface LlmCall {
  model?: string;
  tokensIn?: number;
  tokensOut?: number;
  costUsd?: number;
  /** "id@version", as prompt() returns it. */
  prompt?: string;
  text?: string;
  finishReason?: string;
  /** The tools the model was offered: names, or their definitions as you passed them (Anthropic's
   * input_schema, OpenAI's function.parameters, or an MCP tools/list entry's inputSchema). With
   * definitions, a changed description or schema shows in `assay diff`. */
  tools?: Array<string | ToolDefinition>;
  /** The tool calls the model asked for. */
  toolCalls?: Array<{ name: string; arguments: Record<string, unknown>; id?: string }>;
  tokensCached?: number;
  tokensReasoning?: number;
  /** When the call started (defaults to when it's recorded). */
  started?: Date;
  error?: string;
}

export interface ToolDefinition {
  name?: string;
  description?: string;
  input_schema?: Record<string, unknown>;
  inputSchema?: Record<string, unknown>;
  parameters?: Record<string, unknown>;
  function?: { name: string; description?: string; parameters?: Record<string, unknown> };
}

export class Expectations {
  mustCall(tool: string): this;
  mustNotCall(tool: string): this;
  mustCallBefore(first: string, then: string): this;
  /** An approved run.approval(action) comes before the first call to `action`. */
  mustGetApprovalBefore(action: string): this;
  maxSteps(n: number): this;
  mustAnswer(containing?: string): this;
  /** Records each rule as a check of the run; returns what failed. assayCase calls it. */
  verify(): string[];
}

export class Run {
  readonly id: string;
  readonly task: string;
  readonly caseId?: string;
  /** What was recorded, in order: for the test's own asserts. */
  readonly steps: Step[];
  answerText?: string;
  outcomeValue?: string;
  /** A tool call already made. */
  tool(name: string, args?: Record<string, unknown>, result?: unknown, options?: { error?: string }): void;
  /** Calls fn(args), records it as a tool call (result or error) and returns its result. */
  call<A extends Record<string, unknown>, T>(name: string, fn: (args: A) => T | Promise<T>, args?: A): Promise<T>;
  /** A model call. */
  llm(call?: LlmCall): void;
  /** A decision to allow an action; contracts (requires_approval) and mustGetApprovalBefore() check it. */
  approval(action: string, decision?: "approved" | "rejected" | "pending", options?: { by?: string; reason?: string }): void;
  answer(text: string): void;
  outcome(value: "resolved" | "unresolved" | "escalated"): void;
  /** A check of the run's own: a domain rule, or a test's assert. */
  check(field: string, passed: boolean, reason?: string, evaluator?: string): void;
  /** What the run must do, checked when the case ends. */
  expect(): Expectations;
  end(status?: "completed" | "failed", error?: string): void;
}

/**
 * One test as an Assay case: opens a run named after the current Jest or Vitest test, gives it to
 * fn, then checks its expectations, records whether the test passed, and ends the run.
 */
export function assayCase<T>(name: string, fn: (run: Run) => T | Promise<T>,
                             options?: { tags?: Record<string, Scalar> }): Promise<T>;
export function assayCase<T>(fn: (run: Run) => T | Promise<T>): Promise<T>;
/** The test's context names the case, with node:test or Vitest without globals:
 * test("refund", (t) => assayCase(t, async (run) => ...)). */
export function assayCase<T>(context: { name?: string; fullName?: string; expect?: unknown }, fn: (run: Run) => T | Promise<T>,
                             options?: { tags?: Record<string, Scalar> }): Promise<T>;

export interface RunOptions {
  caseId?: string;
  tags?: Record<string, Scalar>;
  kind?: "agent";
  /** What the run was asked, e.g. the customer's message. */
  input?: unknown;
  /** What it ran on, e.g. { prompt: "receptionist@3", model: "claude-opus-5" }. */
  version?: Record<string, string>;
  segment?: string;
  /** The conversation this run is a turn of, and its place in it from 0. */
  conversationId?: string;
  turn?: number;
  /** Who asked, as a pseudonymous id. */
  user?: string;
}

/** A run outside a test case, e.g. recorded by the app itself. */
export function startRun(task: string, options?: RunOptions): Run;

/**
 * One request of the app as a run: opens it, calls fn(run) with it current (an instrument()ed client
 * records its model calls on it), and ends it, failed if fn throws. With ASSAY_URL set (and not
 * ASSAY_PATH) the run is sent to the server in the background.
 */
export function track<T>(task: string, fn: (run: Run) => T | Promise<T>, options?: Omit<RunOptions, "caseId">): Promise<T>;

/** Sends what's queued now (with ASSAY_URL); resolves true once nothing is left. For shutdown hooks. */
export function flush(): Promise<boolean>;

/** A prompt template, registered by version. Returns "id@version", for run.llm({ prompt }). */
export function prompt(id: string, version: string | number, template?: string): string;

/** A case id the server takes (at most 128 characters): a long one keeps its start and a hash of the whole. */
export function caseIdOf(fullId: string): string;

/** {name: input schema} from tool definitions, each with its description under "x-assay-description". */
export function toolSchemas(tools?: Array<string | ToolDefinition>): Record<string, Record<string, unknown>>;

/**
 * Records the model calls an Anthropic or OpenAI client makes (messages.create, chat.completions.create,
 * responses.create) on the current run: an assayCase's, or a track()'s. Returns the same client.
 * Outside a run, and for streams, nothing is recorded.
 */
export function instrument<T>(client: T): T;

/** Vercel AI SDK middleware: wrapLanguageModel({ model, middleware: assayMiddleware() }). */
export function assayMiddleware(): {
  wrapGenerate: (options: { doGenerate: () => Promise<any>; params: any; model: any }) => Promise<any>;
};

/** The run of the assayCase() or track() the calling code runs inside, or undefined. */
export function currentRun(): Run | undefined;

/** What a SQL statement does: its operation, table, the columns it sets and the ones it leaves empty
 * (DEFAULT, NULL, or a parameter bound to null). null for anything that isn't a read or a write. */
export function summarizeSql(sql: string, values?: unknown[]): {
  op: "INSERT" | "UPDATE" | "DELETE" | "SELECT"; table: string; set: string[]; empty: string[];
} | null;

/** Records what plain tests write to Postgres through pg (assay-evals/jest calls it). Pass pg itself, or
 * leave it out for the project's own. Returns whether pg was found. */
export function recordPg(pg?: unknown): boolean;
