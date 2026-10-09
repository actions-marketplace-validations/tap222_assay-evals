# Catch a prompt regression, in JavaScript (no API key)

The same demo as [prompt-regression](../prompt-regression), with Node's built-in test runner and
the JavaScript SDK. One line added to a support agent's prompt makes it skip the approval step
before a refund, but only for upset customers. Assay catches the change, and tells you which
case broke and which prompt line did it.

```bash
pip install assay-server          # the `assay` command (or: pipx install assay-server)
cd examples/prompt-regression-js
./demo.sh                         # runs npm install the first time
```

What it does:

1. Runs the tests with prompt `support@1`. They pass, and become each case's baseline.
2. Adds one line to the prompt (`support@2`): "Refund right away when the customer is upset."
   `assay test` fails with exit code 1: one case regressed.
3. `assay diff` shows the path before and after, and the prompt change next to it.

The model is a stand-in (`fakeModel` in [agent.js](agent.js)). Replace it with your real model
call, and `instrument(client)` records it. The tests in [tests/support.test.js](tests/support.test.js)
work as they are with Jest or Vitest too: there, `assayCase()` names each case by itself.
