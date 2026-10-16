"use strict";
/**
 * Every Jest test recorded, not only those in an assayCase(): add this file to Jest's setup.
 *
 *   // jest.config.js, or "jest" in package.json
 *   setupFilesAfterEnv: ["assay-evals/jest"]
 *
 * Under `assay test` (ASSAY_PATH set), each test is a case named "<file>::<test name>" with its pass
 * or fail, its time, and what it wrote to Postgres through pg (pg.js). Plain `npx jest` records
 * nothing. Such a test is tagged assay_record = "plain", as with pytest --assay-all: Assay's agent
 * checks are for agents, so only its own asserts and whether it finished judge it.
 *
 * Writes made in the test's own beforeEach and afterEach are the test's too: they run inside it.
 * Those in beforeAll and afterAll aren't recorded.
 */
const { Run, caseIdOf, currentTest, setPlainRun } = require("./index");
const { recordPg } = require("./pg");

const PLAIN = { assay_record: "plain" };

/** Jest's (jest-circus's) state, where the running test keeps its errors; null if it isn't there. */
function circus() {
  const key = Object.getOwnPropertySymbols(globalThis).find((s) => s.description === "JEST_STATE_SYMBOL");
  return key ? globalThis[key] : null;
}

/** The running test's errors as text, or null when they can't be read. */
function errors() {
  const test = circus() && circus().currentlyRunningTest;
  if (!test || !Array.isArray(test.errors)) return null;
  return test.errors.map((e) => {
    const err = Array.isArray(e) ? e[0] : e;
    return String(err && err.message ? err.message : err);
  });
}

let warned = false;

if (process.env.ASSAY_PATH && typeof beforeEach === "function") {
  recordPg();
  let run = null;
  beforeEach(() => {
    const t = currentTest();
    if (!t) return;
    run = new Run(t.name, { caseId: caseIdOf(t.id), tags: PLAIN });
    setPlainRun(run);
  });
  afterEach(() => {
    if (!run) return;
    const errs = errors();
    if (errs === null) {
      if (!warned) process.stderr.write("assay-evals/jest: can't read whether tests passed (needs jest-circus, " +
                                        "Jest's default runner); recording what they did only\n");
      warned = true;
      run.end("completed");
    } else {
      const reason = errs.length ? errs.join("\n").slice(0, 2000) : undefined;
      run.check("test", !errs.length, reason);
      run.end(errs.length ? "failed" : "completed", reason);
    }
    setPlainRun(null);
    run = null;
  });
}
