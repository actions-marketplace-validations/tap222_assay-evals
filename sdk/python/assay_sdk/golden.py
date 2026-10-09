"""Examples from the golden set for a judge to learn from: its train split, with critiques.

    from assay_sdk import golden_examples

    SHOTS = golden_examples(split="train", k=8)     # [{"input", "output", "label", "critique"}]
    PROMPT = RUBRIC + "\\n\\n" + "\\n\\n".join(f"Answer: {x['output']}\\nVerdict: {x['label']}\\nWhy: {x['critique']}"
                                              for x in SHOTS)

A judge built from the items it's then measured on says nothing about unseen data. So take examples
from train only: assay calibrate reports on dev (and test with --final), and a judge that read the
split it's measured on fails calibration as a leak.
"""
from __future__ import annotations

import json
from pathlib import Path
from statistics import median
from typing import List, Optional, Set

_requested: Set[str] = set()


def requested() -> Set[str]:
    """The splits golden_examples has been asked for in this process."""
    return set(_requested)


def reset() -> None:
    """Forget what was asked for: done before each judge is loaded for a calibration."""
    _requested.clear()


def golden_examples(path: str = "golden.jsonl", split: str = "train", k: Optional[int] = None) -> List[dict]:
    """The labeled items of one split: {"id", "input", "output", "label", "critique", "tags"}."""
    _requested.add(split)
    p = Path(path)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        x = json.loads(line)
        if x.get("split") != split:
            continue
        labels = list(x.get("labels") or []) + ([{"score": x["score"], "critique": x.get("critique")}]
                                                if x.get("score") is not None else [])
        scores = [lab["score"] for lab in labels if isinstance(lab.get("score"), (int, float))]
        crit = next((lab.get("critique") for lab in labels if lab.get("critique")), None)
        out.append({"id": x.get("id"), "input": x.get("input"), "output": x.get("output"),
                    "label": median(scores) if scores else None, "critique": crit, "tags": x.get("tags") or []})
    return out[:k] if k else out
