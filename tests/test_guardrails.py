"""Which evaluators could be guardrails (assay/guardrails.py): fast, deterministic, and rarely failing a good output,
by people's labels; the rest stay after the fact. Assay measures; it never blocks."""
import json
from datetime import datetime, timedelta

import pytest

import assay_sdk as assay
from assay import store
from assay.__main__ import main
from assay_sdk.testing import expect


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "assay.toml").write_text('[test]\ncommand = "true"\n\n[calibrate]\nscore_range = [1, 5]\nthreshold = 3\n'
                                         '\n[guardrails]\nmax_ms = 50\nmax_false_positive = 0.05\n', encoding="utf-8")
    (tmp_path / ".assay").mkdir()
    e = store.make_engine(f"sqlite:///{tmp_path / '.assay' / 'assay.db'}")
    now = datetime.utcnow()
    rows, golden = [], []
    for i in range(30):
        case, bad = f"case-{i}", i >= 25  # 25 good outputs, 5 bad, as people labeled them
        golden.append({"id": case, "run": "r1", "input": "q", "output": "a", "labels": [{"by": "sam", "score": 1 if bad else 5}]})
        base = dict(tenant="local", run_id="r1", case_id=case, ts=now, attempt=0, judge_model=None, score=None,
                    reason=None, cost_usd=None)
        rows.append({**base, "result_id": f"o{i}", "field": "has_order_id", "evaluator": "pytest", "duration_ms": 0.2,
                     "status": "fail" if bad and i != 29 else "pass"})  # catches 4 of 5 bad, fails no good one
        rows.append({**base, "result_id": f"s{i}", "field": "short_enough", "evaluator": "pytest", "duration_ms": 0.1,
                     "status": "fail" if i in (1, 2, 3) or bad else "pass"})  # fails 3 good ones
        rows.append({**base, "result_id": f"h{i}", "field": "helpful", "evaluator": "assay.judge@1",
                     "judge_model": "claude-opus-5", "score": 2 if bad else 4, "duration_ms": 2400.0, "cost_usd": 0.004,
                     "status": "fail" if bad else "pass"})
        rows.append({**base, "result_id": f"t{i}", "run_id": "r2", "field": "tone", "evaluator": "pytest",
                     "duration_ms": 0.3, "status": "pass"})  # nothing labeled from r2
    with e.begin() as conn:
        conn.execute(store.eval_results.insert(), rows)
        conn.execute(store.calibrations.insert().values(
            tenant="local", run_id="cal-1", created_at=now - timedelta(days=2), judge="evals/judges.py:helpful", golden="x",
            passed=True, result={"calibration": {"field": "helpful", "n": 30, "catch": {
                "good": 25, "confirmed": 22, "bad": 5, "caught": ["a", "b", "c", "d"], "tnr": 0.8, "tpr": 0.88}}}))
    (tmp_path / "golden.jsonl").write_text("".join(json.dumps(x) + "\n" for x in golden), encoding="utf-8")
    return tmp_path


def test_what_could_run_in_the_request_path(project, capsys):
    assert main(["evals", "guardrails", "--export", "guardrails.json"]) == 0
    out = capsys.readouterr().out
    could, cant, keep = (out.split("Could run in the request path:")[1].split("Can't tell yet:")[0],
                         out.split("Can't tell yet:")[1].split("Keep them after the fact:")[0],
                         out.split("Keep them after the fact:")[1])
    assert "has_order_id: 0.2 ms p95, 0% false positives on 25 good" in could
    assert "tone: 0 good outputs labeled" in cant
    assert "short_enough: 12% false positives, over 5%" in keep
    assert "helpful: an LLM judge" in keep and "2.4 s p95, over 50 ms" in keep
    exported = json.loads((project / "guardrails.json").read_text(encoding="utf-8"))["candidates"]
    assert [x["field"] for x in exported] == ["has_order_id"] and exported[0]["false_negative"] == 0.2


def test_the_stakes_decide_the_thresholds(project, capsys):
    (project / "assay.toml").write_text('[test]\ncommand = "true"\n\n[calibrate]\nscore_range = [1, 5]\nthreshold = 3\n'
                                        '\n[guardrails]\nmax_ms = 5000\nmax_false_positive = 0.15\nmax_false_negative = 0.1\n'
                                        'min_labeled = 5\njudges = true\n', encoding="utf-8")
    assert main(["evals", "guardrails", "--format", "json"]) == 0
    r = {x["field"]: x for x in json.loads(capsys.readouterr().out)["evaluators"]}
    assert r["has_order_id"]["verdict"] == "not a candidate" and "20% false negatives" in r["has_order_id"]["why"]
    assert r["helpful"]["verdict"] == "not a candidate"  # its calibration: 3 of 25 good failed, 1 of 5 bad passed
    assert (r["helpful"]["false_positive"], r["helpful"]["false_negative"], r["helpful"]["errors_from"]) == (0.12, 0.2, "calibration")
    assert r["short_enough"]["verdict"] == "candidate"  # 12% false positives is fine at these stakes


def test_evaluators_record_how_long_they_took():
    sent = []
    assay.init(transport=lambda b: sent.extend(b), flush_interval=60)
    try:
        with assay.run("t", test="case-1") as r:
            r.answer("ok")
            assay.evaluate(lambda a: {"score": 1.0}, "ok", run=r, field="judged")
            expect(r).must_answer().verify()
        assay.flush()
    finally:
        assay.init(enabled=False)
    checks = {c["field"]: c for c in sent if c["type"] == "check"}
    assert checks["judged"]["duration_ms"] >= 0 and checks["expect.must_answer()"]["duration_ms"] >= 0
