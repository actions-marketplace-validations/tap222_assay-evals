"""Experts' decisions on single claims (assay_sdk.claim_review), made while they use the product.

"Was the answer helpful?" says little about what went wrong. A clinician checking a research
assistant's claims against the evidence it links, and correcting one or resolving a conflict between
two sources, says exactly what. Those decisions are:

  labels     golden() turns supported and wrong claims into golden-set items (score 1 or 0, the
             correction as the critique), to calibrate the faithfulness judge against the experts
             (assay calibrate, label range 0 to 1)
  signals    a claim marked wrong is a failure signal for assay learn, like a reported error
  findings   summary() counts them per verdict for the report

They're kept under the same limits as the traces: redacted before they're sent (init(redact=...)).
"""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import List, Optional

from sqlalchemy import and_, select
from sqlalchemy.engine import Engine

from assay import store


def reviews(engine: Engine, tenant: str, since: Optional[datetime] = None,
            until: Optional[datetime] = None) -> List[dict]:
    c = store.claim_reviews
    cond = [c.c.tenant == tenant]
    if since is not None:
        cond.append(c.c.ts >= since)
    if until is not None:
        cond.append(c.c.ts < until)
    with engine.connect() as conn:
        return [dict(r._mapping) for r in conn.execute(select(c).where(and_(*cond)).order_by(c.c.ts))]


def golden(engine: Engine, tenant: str, since: Optional[datetime] = None) -> List[dict]:
    """Golden-set items from claims an expert called supported (1) or wrong (0): the claim as the
    output, the question and the evidence as the input. Conflicts resolved and unsure ones aren't
    labels of the claim."""
    from assay import learn
    rows = [r for r in reviews(engine, tenant, since) if r["verdict"] in ("supported", "wrong")]
    asked = learn._inputs(engine, tenant, sorted({r["run_id"] for r in rows}))
    out = []
    for r in rows:
        item = {"id": f"claim-{r['review_id']}", "input": {"question": (asked.get(r["run_id"]) or {}).get("input"),
                                                          "evidence": r["evidence"] or []},
                "output": r["claim"], "score": 1 if r["verdict"] == "supported" else 0,
                "by": r["by"] or "expert", "tags": ["claim", r["verdict"]]}
        if r["correction"] or r["note"]:
            item["critique"] = " ".join(x for x in (r["note"], f"It should say: {r['correction']}" if r["correction"]
                                                    else None) if x)
        out.append(item)
    return out


def summary(engine: Engine, tenant: str, since: datetime, until: datetime) -> dict:
    rows = reviews(engine, tenant, since, until)
    n = Counter(r["verdict"] for r in rows)
    return {"reviewed": len(rows), "supported": n["supported"], "wrong": n["wrong"],
            "conflict_resolved": n["conflict_resolved"], "unsure": n["unsure"],
            "runs": len({r["run_id"] for r in rows}), "experts": len({r["by"] for r in rows if r["by"]}),
            "wrong_examples": [r["claim"][:200] for r in rows if r["verdict"] == "wrong"][-3:]}


def pull(root, url: str, source: str, days, golden_path: str = "golden.jsonl") -> int:
    """`assay golden claims`: the server's claim labels appended to the golden set, none twice."""
    import json
    import os
    import sys
    from pathlib import Path
    from assay import local
    key = os.environ.get("ASSAY_KEY")
    q = f"source={source}" + (f"&days={days}" if days else "")
    code, body = local._http("GET", f"{url.rstrip('/')}/v1/claims/golden?{q}", None,
                             {"Authorization": f"Bearer {key}"} if key else {})
    if code != 200:
        print(f"{url} refused ({code}): {body}", file=sys.stderr)
        return 2
    path = Path(root) / golden_path
    have = set()
    if path.exists():
        have = {json.loads(x).get("id") for x in path.read_text().splitlines() if x.strip()}
    new = [x for x in body["items"] if x["id"] not in have]
    if new:
        with path.open("a") as f:
            for x in new:
                f.write(json.dumps(x, ensure_ascii=False, default=str) + "\n")
    print(f"{len(new)} claim label{'s' * (len(new) != 1)} from experts added to {golden_path} "
          f"({len(body['items']) - len(new)} there already). Calibrate the faithfulness judge on them with "
          "score_range = [0, 1].")
    return 0
