"""The JavaScript SDK (sdk/js) under `assay test`: a Node project's cases are checked and compared
with their last passing run like pytest's."""
import json
import shutil
from pathlib import Path

import pytest

from assay.__main__ import main
from tests.test_local import config, project  # noqa: F401

JS_SDK = Path(__file__).resolve().parents[1] / "sdk" / "js"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="needs Node")

AGENT = """
const {{ assayCase }} = require({sdk});
const skipApproval = process.env.SKIP_APPROVAL === "1";

async function support(run, orderId) {{
  run.expect().mustCall("get_order").mustCallBefore("approval", "refund");
  const order = await run.call("get_order", async () => ({{ price: 27.61, status: "delivered" }}), {{ orderId }});
  run.llm({{ model: "demo-model", tokensIn: 850, tokensOut: 60 }});
  if (!skipApproval) run.tool("approval", {{ action: "refund" }}, {{ decision: "approved" }});
  await run.call("refund", async () => ({{ refunded: order.price }}), {{ orderId, amount: order.price }});
  run.answer(`Refunded $${{order.price}}.`);
  run.outcome("resolved");
}}

(async () => {{
  let failed = 0;
  for (const id of ["O-17", "O-18"]) {{
    try {{ await assayCase(`refund ${{id}}`, (run) => support(run, id)); }} catch (e) {{ failed++; }}
  }}
  process.exit(failed ? 1 : 0);
}})();
"""


def test_a_node_project_is_tested_and_a_regression_caught(project, capsys, monkeypatch):  # noqa: F811
    (project / "agent.js").write_text(AGENT.format(sdk=json.dumps(str(JS_SDK))))
    config(project, "node agent.js")
    assert main(["test"]) == 0
    out = capsys.readouterr().out
    assert "✓ 2 passed" in out and "✓ Your asserts" in out

    monkeypatch.setenv("SKIP_APPROVAL", "1")  # the refund no longer waits for approval
    assert main(["test"]) == 1
    capsys.readouterr()
    assert main(["diff"]) == 1
    out = capsys.readouterr().out
    assert "2 regressed" in out and "No longer: approval" in out
    assert "expect.must_call_before(approval, refund)" in out
