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
  /** Names of the tools the model was offered. */
  tools?: string[];
  error?: string;
}

export class Expectations {
  mustCall(tool: string): this;
  mustNotCall(tool: string): this;
  mustCallBefore(first: string, then: string): this;
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

/** A run outside a test case, e.g. recorded by the app itself. */
export function startRun(task: string, options?: { caseId?: string; tags?: Record<string, Scalar>; kind?: "agent" }): Run;

/** A prompt template, registered by version. Returns "id@version", for run.llm({ prompt }). */
export function prompt(id: string, version: string | number, template?: string): string;

/** A case id the server takes (at most 128 characters): a long one keeps its start and a hash of the whole. */
export function caseIdOf(fullId: string): string;
