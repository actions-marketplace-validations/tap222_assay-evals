#!/usr/bin/env bash
# Catch a prompt regression in under a minute, in JavaScript, with no API key.
set -u
cd "$(dirname "$0")"
rm -rf .assay
[ -d node_modules/assay-evals ] || npm install --silent

echo "== 1. Baseline: prompt support@1 =="
ASSAY_DEMO_PROMPT=1 assay test

echo
echo "== 2. One line added to the prompt: support@2 =="
diff prompts/support-1.txt prompts/support-2.txt
ASSAY_DEMO_PROMPT=2 assay test
echo "(exit code $?: 1 means a regression)"

echo
echo "== 3. What changed =="
assay diff
