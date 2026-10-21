# Antigravity + Assay: Behavioral Regression Testing for AI Agents

This example shows how **Google Antigravity** and **Assay** pair program to catch prompt and agent behavioral regressions before committing or deploying.

---

### How It Works

1. **Agent Skill Installed:**
   The workspace includes `.agent/skills/assay/SKILL.md` (or installed via `assay init --antigravity`).
2. **Behavioral Baseline:**
   Each test running under `pytest --assay` is compared against its last passing execution path (tool sequence, approvals, constraints, and answers).
3. **Automated Regression Detection:**
   When Antigravity modifies a prompt or pipeline logic, it runs `pytest -q --assay tests`. If a prompt change alters tool execution paths (such as skipping an approval gate for upset customers), Assay fails with exit code `1`.
4. **Actionable Root Cause Diff:**
   Running `assay diff` pinpoints the exact prompt line change and the omitted step. Antigravity reads the diff and proposes a fix.

---

### Run the Demo

```bash
cd examples/antigravity
./demo.sh
```

#### What `demo.sh` does:
1. Runs tests with `support@1`. All cases pass and establish their baselines.
2. Updates prompt to `support@2` ("Refund right away when the customer is upset").
3. `pytest --assay` detects that `test_refund_upset_customer` skipped `approval(refund)`:
   ```text
   ✓ 2 unchanged
   ✗ 1 regressed

   CHANGED IN EVERY CASE
     prompt  support@1 → support@2 (+1 line, −0: “Refund right away when the customer is upset.”)

   REGRESSIONS
   1. test_refund_upset_customer
      Expected: get_order → approval(refund) → refund
      Actual:   get_order → refund
      No longer: approval(refund)
      Severity: HIGH
   ```
4. Antigravity catches this behavioral shift and corrects the prompt or code before merge.
