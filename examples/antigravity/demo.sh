#!/usr/bin/env bash
# Antigravity + Assay: catch a prompt regression in under a minute
set -u
cd "$(dirname "$0")"
rm -rf .assay

echo "== 1. Baseline: prompt support@1 =="
ASSAY_DEMO_PROMPT=1 pytest -q --assay tests

echo
echo "== 2. One line added to the prompt: support@2 =="
diff prompts/support-1.txt prompts/support-2.txt
ASSAY_DEMO_PROMPT=2 pytest -q --assay tests
echo "(exit code $?: 1 means a behavioral regression was detected)"

echo
echo "== 3. What changed (assay diff) =="
assay diff
