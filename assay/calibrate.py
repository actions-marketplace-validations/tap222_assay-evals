"""Judge calibration: does your judge's score track a person's, and does it still after a change?

A judge can return valid JSON, in range, every time, and still be wrong: rank a great answer
below a poor one, call everything a 3, or give the same answer a 2 and a 4 on two tries.
Checking that takes a golden set: outputs a person scored, spanning terrible to great. This
runs your judge over it, several times per item, and says:

  ranking      Spearman rank correlation with the labels (and its 95% interval, which says how
               much a set this size can tell you), the share of pairs in the right order, and
               the ordering violations themselves: labeled 5, judged below one labeled 2
  agreement    exact and within one, and label × judge
  bias         lenient or harsh on average, per label, and whether it squashes the scale
  consistency  how much each item's score swings across repeats, and which flip pass/fail
  validity     answers that weren't a verdict, timeouts: counted apart, never as scores

Each calibration is stored, and compared with the last one that passed: overall and per tag
(a question type, say), since improving one kind of item often breaks another. It regressed
when a rank correlation dropped beyond chance (a paired bootstrap over the items both runs
judged) by at least min_drop, or when a new ordering violation is two or more label points
apart. Smaller moves are reported, not failed.

golden.jsonl, one item a line:

  {"id": "q17", "input": "...", "output": "...", "score": 4, "by": "sam", "tags": ["behavioral"]}
  {"id": "q18", "input": "...", "output": "...", "labels": [{"by": "sam", "score": 2}, {"by": "ana", "score": 3}]}

An item's label is the median of its labels. Items two people labeled say how much people agree:
the ceiling for any judge.

Three more things say whether a judge can be trusted:

  variants     versions of an item's output with what should happen to its score: a paraphrase
               should keep it ("same"), a subtly broken answer should lose it ("lower"):
               {"id": "q17", ..., "variants": [{"output": "...", "expect": "same", "note": "paraphrase"}]}
  second judge another judge over the same items (second_judge): how often they agree, and the
               items they disagree on, which is usually where the rubric is ambiguous
  drift        the same judge (its code, and the models that answered) over the same items,
               agreeing with people less than it did: the provider changed the model under its
               name. Only a calibration run on a schedule, with nothing else changed, catches it.
"""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import math
import random
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import mean, median, pstdev
from typing import Any, Callable, Dict, List, Optional, Tuple

BOOTSTRAP, SEED = 1000, 0
MIN_TAG_ITEMS = 8  # fewer can't say whether a tag got worse
DEFAULTS = {"golden": "golden.jsonl", "judge": None, "repeat": 5, "score_range": [1, 5], "label_range": None,
            "threshold": None, "concurrency": 8, "min_drop": 0.05, "field": None, "second_judge": None}
EXPECTS = ("same", "lower", "higher")


class CalibrationError(ValueError):
    pass


# ---------- the golden set ----------

def load_golden(path: Path) -> List[dict]:
    if not path.exists():
        raise CalibrationError(f"No golden set at {path}. `assay golden add CASE --score N` starts one from a "
                               f"recorded run, or write it: one JSON object a line, with input, output and score.")
    items, seen = [], set()
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.strip():
            continue
        try:
            x = json.loads(line)
        except ValueError:
            raise CalibrationError(f"{path.name}, line {n}: not JSON.")
        if not isinstance(x, dict) or "output" not in x:
            raise CalibrationError(f"{path.name}, line {n}: needs at least output, and a score or labels.")
        labels = list(x.get("labels") or [])
        if x.get("score") is not None:
            labels.insert(0, {"by": x.get("by") or "unknown", "score": x["score"]})
        labels = [lab for lab in labels if isinstance(lab, dict) and isinstance(lab.get("score"), (int, float))
                  and not isinstance(lab.get("score"), bool)]
        if not labels:
            raise CalibrationError(f"{path.name}, line {n}: no score. Give score (and by), or labels.")
        x["id"] = str(x.get("id") or f"item-{n}")
        if x["id"] in seen:
            raise CalibrationError(f"{path.name}, line {n}: id {x['id']!r} is there twice.")
        seen.add(x["id"])
        x["labels"], x["label"] = labels, median(lab["score"] for lab in labels)
        x["tags"] = [str(t) for t in x.get("tags") or []]
        vs = x.get("variants") or []
        if not isinstance(vs, list) or any(not isinstance(v, dict) or "output" not in v or v.get("expect") not in EXPECTS
                                           for v in vs):
            raise CalibrationError(f"{path.name}, line {n}: each variant needs output, and expect: same, lower or "
                                   f"higher (what should happen to the score).")
        x["variants"] = vs
        items.append(x)
    if not items:
        raise CalibrationError(f"{path.name} is empty.")
    return items


def save_golden(path: Path, items: List[dict]) -> None:
    out = []
    for x in items:
        x = {k: v for k, v in x.items() if k != "label"}
        labs = x.pop("labels", [])
        x.pop("score", None), x.pop("by", None)
        if len(labs) == 1:
            x["score"], x["by"] = labs[0]["score"], labs[0].get("by")
        else:
            x["labels"] = labs
        out.append(json.dumps(x, ensure_ascii=False, default=str))
    path.write_text("\n".join(out) + "\n")


def digest(items: List[dict]) -> str:
    return hashlib.sha256(json.dumps([[x["id"], x["label"]] for x in items]).encode()).hexdigest()[:12]


def coverage(items: List[dict], label_range: Tuple[float, float]) -> dict:
    """Labels per level, labelers, and how much labelers agree where two labeled the same item."""
    lo, hi = label_range
    levels = list(range(int(math.ceil(lo)), int(math.floor(hi)) + 1))
    per = Counter(int(round(x["label"])) for x in items)
    by = Counter(lab.get("by") or "unknown" for x in items for lab in x["labels"])
    pairs = [(x["labels"][0]["score"], x["labels"][1]["score"]) for x in items if len(x["labels"]) >= 2]
    agree = None
    if pairs:
        agree = {"items": len(pairs), "exact": sum(a == b for a, b in pairs) / len(pairs),
                 "within_one": sum(abs(a - b) <= 1 for a, b in pairs) / len(pairs),
                 "spearman": spearman([a for a, _ in pairs], [b for _, b in pairs]) if len(pairs) >= 3 else None}
    tags = Counter(t for x in items for t in x["tags"])
    variants = Counter(v["expect"] for x in items for v in x.get("variants") or [])
    return {"items": len(items), "levels": {lv: per.get(lv, 0) for lv in levels},
            "missing": [lv for lv in levels if not per.get(lv)], "labelers": dict(by.most_common()),
            "labeler_agreement": agree, "tags": dict(tags.most_common()), "variants": dict(variants)}


# ---------- statistics ----------

def _ranks(xs: List[float]) -> List[float]:
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    r = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2 + 1
        i = j + 1
    return r


def spearman(a: List[float], b: List[float]) -> Optional[float]:
    """Rank correlation, ties averaged; None when either side doesn't vary."""
    if len(a) < 3:
        return None
    ra, rb = _ranks(a), _ranks(b)
    ma, mb = mean(ra), mean(rb)
    num = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    den = math.sqrt(sum((x - ma) ** 2 for x in ra) * sum((y - mb) ** 2 for y in rb))
    return num / den if den else None


def _interval(values: List[Optional[float]]) -> Optional[Tuple[float, float]]:
    vs = sorted(v for v in values if v is not None)
    if len(vs) < BOOTSTRAP // 2:
        return None
    return vs[int(0.025 * len(vs))], vs[int(0.975 * len(vs)) - 1]


def bootstrap_rho(labels: List[float], judged: List[float]) -> Optional[Tuple[float, float]]:
    rnd, n = random.Random(SEED), len(labels)
    if n < 5:
        return None
    out = []
    for _ in range(BOOTSTRAP):
        idx = [rnd.randrange(n) for _ in range(n)]
        out.append(spearman([labels[i] for i in idx], [judged[i] for i in idx]))
    return _interval(out)


def paired_drop(labels: List[float], before: List[float], now: List[float]) -> Optional[Tuple[float, float]]:
    """95% interval of rho(now) - rho(before) over the same items, resampled together."""
    rnd, n = random.Random(SEED), len(labels)
    if n < 5:
        return None
    out = []
    for _ in range(BOOTSTRAP):
        idx = [rnd.randrange(n) for _ in range(n)]
        la = [labels[i] for i in idx]
        a, b = spearman(la, [before[i] for i in idx]), spearman(la, [now[i] for i in idx])
        out.append(None if a is None or b is None else b - a)
    return _interval(out)


def violations(items: List[dict]) -> Tuple[List[dict], int]:
    """Pairs a person ordered one way and the judge the other: ([{"high", "low", "gap"}], comparable pairs)."""
    judged = [x for x in items if x.get("judged") is not None]
    out, comparable = [], 0
    for i, a in enumerate(judged):
        for b in judged[i + 1:]:
            if a["label"] == b["label"]:
                continue
            hi, lo = (a, b) if a["label"] > b["label"] else (b, a)
            comparable += 1
            if hi["judged"] < lo["judged"]:
                out.append({"high": hi["id"], "low": lo["id"], "gap": hi["label"] - lo["label"],
                            "high_label": hi["label"], "high_judged": hi["judged"],
                            "low_label": lo["label"], "low_judged": lo["judged"]})
    return sorted(out, key=lambda v: (-v["gap"], v["high_judged"] - v["low_judged"])), comparable


# ---------- running the judge ----------

def load_judge(spec: str, root: Path) -> Callable:
    """"evals/judges.py:helpfulness" or "evals.judges:helpfulness"."""
    if not spec or ":" not in spec:
        raise CalibrationError("[calibrate] judge: the function to calibrate, as \"evals/judges.py:helpfulness\" "
                               "or \"evals.judges:helpfulness\". It's called as judge(input, output).")
    where, name = spec.rsplit(":", 1)
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    try:
        if where.endswith(".py"):
            s = importlib.util.spec_from_file_location(Path(where).stem, root / where)
            if s is None or not (root / where).exists():
                raise CalibrationError(f"[calibrate] judge: no file {where}.")
            mod = importlib.util.module_from_spec(s)
            s.loader.exec_module(mod)
        else:
            mod = importlib.import_module(where)
    except CalibrationError:
        raise
    except Exception as exc:
        raise CalibrationError(f"[calibrate] judge: couldn't load {where}: {type(exc).__name__}: {exc}")
    fn = getattr(mod, name, None)
    if not callable(fn):
        raise CalibrationError(f"[calibrate] judge: {where} has no function {name}.")
    return fn


def fingerprint(judge: Callable) -> Optional[str]:
    """The judge's code, as a digest of its source file: an edited rubric is another judge."""
    import inspect
    try:
        return hashlib.sha256(Path(inspect.getsourcefile(judge)).read_bytes()).hexdigest()[:12]
    except (TypeError, OSError):
        return None


def _to_labels(s: float, score_range, label_range) -> float:
    (a, b), (c, d) = score_range, label_range
    return s if (a, b) == (c, d) else c + (s - a) * (d - c) / (b - a)


def run_judge(judge: Callable, items: List[dict], cfg: dict, rt=None):
    """Every item `repeat` times through an EvalRuntime: {id: [Result]}, and the runtime's report."""
    from assay_sdk import EvalRuntime, Sample
    rt = rt or EvalRuntime(concurrency=int(cfg["concurrency"]), retries=2)
    samples = [Sample(x.get("input"), x["output"], id=f"{x['id']}#{k}", **(x.get("args") or {}))
               for x in items for k in range(int(cfg["repeat"]))]
    samples += [Sample(x.get("input"), v["output"], id=f"{x['id']}~{j}#{k}", **(x.get("args") or {}))
                for x in items for j, v in enumerate(x.get("variants") or []) for k in range(int(cfg["repeat"]))]
    lo, hi = cfg["score_range"]
    report = rt.run(judge, samples, score_range=(lo, hi), threshold=cfg["threshold"] if cfg["threshold"] is not None
                    else (lo + hi) / 2)
    by: Dict[str, list] = defaultdict(list)
    for r in report.results:
        by[str(r.id).rsplit("#", 1)[0]].append(r)
    return by, report


# ---------- the analysis ----------

def analyze(items: List[dict], results: Dict[str, list], cfg: dict) -> dict:
    score_range = tuple(cfg["score_range"])
    label_range = tuple(cfg["label_range"] or cfg["score_range"])
    lo, hi = score_range
    threshold = cfg["threshold"] if cfg["threshold"] is not None else (lo + hi) / 2
    invalid, first_error = Counter(), None
    rows = []
    for x in items:
        rs = results.get(x["id"], [])
        scores = [r.result.score for r in rs if r.result is not None and r.result.valid and r.result.score is not None]
        for r in rs:
            if r.result is None or not r.result.valid:
                invalid[r.kind] += 1
                first_error = first_error or f"{x['id']}: {r.error}"
        conv = [_to_labels(s, score_range, label_range) for s in scores]
        rows.append({**x, "scores": scores, "judged": median(conv) if conv else None,
                     "spread": (max(conv) - min(conv)) if len(conv) > 1 else 0.0 if conv else None,
                     "sd": pstdev(conv) if len(conv) > 1 else 0.0 if conv else None,
                     "flips": len({s >= threshold for s in scores}) > 1})
    judged = [r for r in rows if r["judged"] is not None]
    L, J = [r["label"] for r in judged], [r["judged"] for r in judged]
    span = label_range[1] - label_range[0]
    variants = []
    for r in judged:
        for j, v in enumerate(r.get("variants") or []):
            vs = [_to_labels(x.result.score, score_range, label_range) for x in results.get(f"{r['id']}~{j}", [])
                  if x.result is not None and x.result.valid and x.result.score is not None]
            if not vs:
                continue
            vm = median(vs)
            noise = max(r["spread"] or 0, max(vs) - min(vs), 0.1 * span)
            ok = abs(vm - r["judged"]) <= noise if v["expect"] == "same" else \
                vm < r["judged"] - noise / 2 if v["expect"] == "lower" else vm > r["judged"] + noise / 2
            variants.append({"id": r["id"], "variant": j, "expect": v["expect"], "note": v.get("note"),
                             "original": r["judged"], "judged": vm, "ok": ok})
    models = sorted({x.result.judge_model for rs in results.values() for x in rs
                     if x.result is not None and getattr(x.result, "judge_model", None)})
    rho = spearman(L, J)
    viol, comparable = violations(judged)
    levels = sorted({int(round(r["label"])) for r in judged})
    per_label = {lv: mean(r["judged"] for r in judged if int(round(r["label"])) == lv) for lv in levels}
    matrix = defaultdict(Counter)
    for r in judged:
        matrix[int(round(r["label"]))][int(round(r["judged"]))] += 1
    sd_l = pstdev(L) if len(L) > 1 else 0
    tags = {}
    for t in sorted({t for r in judged for t in r["tags"]}):
        mine = [r for r in judged if t in r["tags"]]
        tags[t] = {"n": len(mine), "spearman": spearman([r["label"] for r in mine], [r["judged"] for r in mine])}
    return {
        "items": rows, "n": len(items), "judged": len(judged), "invalid": dict(invalid), "first_error": first_error,
        "spearman": rho, "interval": bootstrap_rho(L, J),
        "pairs": {"comparable": comparable, "right": comparable - len(viol), "violations": viol},
        "agreement": {"exact": mean(round(j) == round(lab) for j, lab in zip(J, L)) if J else None,
                      "within_one": mean(abs(j - lab) <= 1 for j, lab in zip(J, L)) if J else None},
        "bias": mean(j - lab for j, lab in zip(J, L)) if J else None, "per_label": per_label,
        "squashed": bool(sd_l and len(J) > 1 and pstdev(J) / sd_l < 0.5),
        "matrix": {k: dict(v) for k, v in matrix.items()},
        "consistency": {"mean_spread": mean(r["spread"] for r in judged) if judged else None,
                        "flips": [r["id"] for r in judged if r["flips"]],
                        "widest": sorted(judged, key=lambda r: -(r["spread"] or 0))[:3]},
        "tags": tags, "coverage": coverage(items, label_range), "label_range": label_range,
        "variants": variants, "models": models, "golden_digest": digest(items),
    }


def between_judges(a: dict, b: dict) -> dict:
    """Two judges over the same items: how often they agree, and where they don't (by 2+ points)."""
    x = {r["id"]: r for r in a["items"] if r.get("judged") is not None}
    y = {r["id"]: r for r in b["items"] if r.get("judged") is not None}
    ids = [i for i in x if i in y]
    A, B = [x[i]["judged"] for i in ids], [y[i]["judged"] for i in ids]
    apart = sorted(({"id": i, "label": x[i]["label"], "first": x[i]["judged"], "second": y[i]["judged"]}
                    for i in ids if abs(x[i]["judged"] - y[i]["judged"]) >= 2), key=lambda d: -abs(d["first"] - d["second"]))
    return {"items": len(ids), "spearman": spearman(A, B),
            "exact": mean(round(p) == round(q) for p, q in zip(A, B)) if ids else None,
            "within_one": mean(abs(p - q) <= 1 for p, q in zip(A, B)) if ids else None,
            "apart": apart, "second_spearman": b["spearman"], "first_spearman": a["spearman"]}


def compare(now: dict, before: dict, cfg: dict) -> dict:
    """Against the baseline calibration: overall and per tag, over the items both judged."""
    def judged(a):
        return {r["id"]: r for r in a["items"] if r.get("judged") is not None}
    a, b = judged(before), judged(now)
    common = [i for i in b if i in a and a[i]["label"] == b[i]["label"]]
    min_drop = float(cfg["min_drop"])
    out = {"items": len(common), "overall": None, "tags": {}, "new_violations": [], "regressed": False}

    def test(ids):
        if len(ids) < 5:
            return None
        L = [b[i]["label"] for i in ids]
        was, now_ = spearman(L, [a[i]["judged"] for i in ids]), spearman(L, [b[i]["judged"] for i in ids])
        iv = paired_drop(L, [a[i]["judged"] for i in ids], [b[i]["judged"] for i in ids])
        worse = bool(was is not None and now_ is not None and iv and iv[1] < 0 and was - now_ >= min_drop)
        return {"before": was, "now": now_, "interval": iv, "n": len(ids), "worse": worse}
    out["overall"] = test(common)
    for t in sorted({t for i in common for t in b[i]["tags"]}):
        ids = [i for i in common if t in b[i]["tags"]]
        if len(ids) >= MIN_TAG_ITEMS:
            out["tags"][t] = test(ids)
    old = {(v["high"], v["low"]) for v in violations([a[i] for i in common])[0]}
    out["new_violations"] = [v for v in violations([b[i] for i in common])[0] if (v["high"], v["low"]) not in old]
    was_ok = {(v["id"], v["variant"]) for v in before.get("variants") or [] if v["ok"]}
    out["new_variant_failures"] = [v for v in now.get("variants") or [] if not v["ok"] and (v["id"], v["variant"]) in was_ok]
    blind = [v for v in out["new_variant_failures"] if v["expect"] == "lower" and v["judged"] >= v["original"]]
    out["regressed"] = bool((out["overall"] or {}).get("worse") or any((t or {}).get("worse") for t in out["tags"].values())
                            or any(v["gap"] >= 2 for v in out["new_violations"]) or blind)
    # Why: the judge changed (its code, its model), or nothing did and the provider's model moved.
    j0, j1 = before.get("judge") or {}, now.get("judge") or {}
    same_items = before.get("golden_digest") == now.get("golden_digest")
    if j0.get("source") and j0.get("source") != j1.get("source"):
        out["cause"] = "the judge's code changed since the baseline"
    elif (before.get("models") or []) != (now.get("models") or []) and before.get("models") and now.get("models"):
        out["cause"] = f"the judge's model changed: {', '.join(before['models'])} → {', '.join(now['models'])}"
    elif out["regressed"] and same_items and j0.get("source") and j0 == j1:
        out["cause"] = ("the same judge (its code and its model's name) over the same items agrees with people less "
                        "than it did: the provider changed the model under its name")
        out["drift"] = True
    return out


# ---------- showing it ----------

def _f(v: Optional[float], nd: int = 2) -> str:
    return "n/a" if v is None else f"{v:.{nd}f}"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v:.0%}"


def _variant(v: dict) -> str:
    what = {"same": "should stay the same", "lower": "should score lower", "higher": "should score higher"}[v["expect"]]
    return f"{v['id']}{' (' + v['note'] + ')' if v.get('note') else ''}: {v['original']:.1f} → {v['judged']:.1f}, {what}"


def text(run_id: str, spec: str, a: dict, cfg: dict, cmp: Optional[dict], baseline: Optional[str],
         runtime_line: Optional[str], paint=lambda s, c: s, second: Optional[dict] = None) -> str:
    cov = a["coverage"]
    out = [paint("Judge calibration", "bold") + f"  {run_id}", "─" * 44,
           f"{spec} · {a['n']} items · {cfg['repeat']} judgements each", ""]
    labelers = ", ".join(f"{k} ({v})" for k, v in cov["labelers"].items())
    out.append(f"Golden set   {cov['items']} items, labeled by {labelers}")
    out.append("             " + "  ".join(f"{lv}: {n}" for lv, n in cov["levels"].items()))
    if cov["missing"]:
        out.append(paint(f"             nothing labeled {', '.join(map(str, cov['missing']))}: the judge is untested "
                         f"there (`assay golden suggest`)", "yellow"))
    ag = cov["labeler_agreement"]
    if ag:
        out.append(f"             people agree: exact {_pct(ag['exact'])}, within one {_pct(ag['within_one'])} "
                   f"({ag['items']} items labeled twice): the ceiling for any judge")
    out.append("")
    iv = a["interval"]
    out.append(f"Ranking      Spearman {_f(a['spearman'])}" + (f" (95% interval {_f(iv[0])}–{_f(iv[1])})" if iv else
                                                                  " (too few items for an interval)"))
    p = a["pairs"]
    if p["comparable"]:
        out.append(f"             pairs in the right order: {_pct(p['right'] / p['comparable'])} "
                   f"({p['right']:,} of {p['comparable']:,})")
    far = [v for v in p["violations"] if v["gap"] >= 2]
    if far:
        out.append(paint(f"             {len(far)} ordering violation{'s' * (len(far) != 1)} two or more apart:", "yellow"))
        out += [f"               {v['high']} labeled {v['high_label']:g}, judged {v['high_judged']:.1f}  <  "
                f"{v['low']} labeled {v['low_label']:g}, judged {v['low_judged']:.1f}" for v in far[:5]]
    ag2 = a["agreement"]
    out.append(f"Agreement    exact {_pct(ag2['exact'])}, within one {_pct(ag2['within_one'])}")
    if a["bias"] is not None:
        lean = "lenient" if a["bias"] > 0.25 else "harsh" if a["bias"] < -0.25 else "no lean"
        worst = max(a["per_label"].items(), key=lambda kv: abs(kv[1] - kv[0]), default=None)
        out.append(f"Bias         {a['bias']:+.2f} ({lean})" + (f" · labeled {worst[0]} → judged {worst[1]:.1f} on "
                                                                 f"average" if worst else "")
                   + (" · squashes the scale toward the middle" if a["squashed"] else ""))
    c = a["consistency"]
    if c["mean_spread"] is not None:
        line = f"Consistency  mean spread {c['mean_spread']:.2f} across {cfg['repeat']} judgements"
        if c["flips"]:
            line += f" · {len(c['flips'])} flip pass/fail: {', '.join(c['flips'][:5])}"
        out.append(line)
    bad = sum(a["invalid"].values())
    out.append(f"Validity     {a['judged']} of {a['n']} items judged" + (
        "; not verdicts: " + ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in a["invalid"].items()) if bad else ""))
    if bad and a.get("first_error"):
        out.append(paint(f"             e.g. {a['first_error'][:200]}", "dim"))
    if a["tags"]:
        out.append("By tag       " + "   ".join(f"{t} {_f(v['spearman'])} (n={v['n']})" for t, v in a["tags"].items()))
    vs = a.get("variants") or []
    if vs:
        ok = sum(v["ok"] for v in vs)
        parts = []
        for e, words in (("same", "paraphrases kept their score"), ("lower", "broken versions scored lower"),
                         ("higher", "improved versions scored higher")):
            mine = [v for v in vs if v["expect"] == e]
            if mine:
                parts.append(f"{words} {sum(v['ok'] for v in mine)}/{len(mine)}")
        out.append(f"Variants     {ok} of {len(vs)} as expected: " + ", ".join(parts))
        out += [paint(f"               {_variant(v)}", "yellow") for v in vs if not v["ok"]][:5]
    if second:
        s = second
        out += ["", f"Second judge {s['spec']} over the same {s['items']} items",
                f"             agrees with the first: Spearman {_f(s['spearman'])}, exact {_pct(s['exact'])}, "
                f"within one {_pct(s['within_one'])}",
                f"             with people: first {_f(s['first_spearman'])}, second {_f(s['second_spearman'])}"]
        if s["apart"]:
            out.append(paint(f"             {len(s['apart'])} item{'s' * (len(s['apart']) != 1)} they disagree on by 2+ "
                             f"points (the rubric is likely ambiguous there):", "yellow"))
            out += [f"               {d['id']}: labeled {d['label']:g}, first {d['first']:.1f}, second {d['second']:.1f}"
                    for d in s["apart"][:5]]
    if a["matrix"]:
        cols = sorted({j for row in a["matrix"].values() for j in row})
        out += ["", "Label × judge  " + " ".join(f"{j:>3}" for j in cols)]
        for lv in sorted(a["matrix"]):
            out.append(f"  {lv:>3}         " + " ".join(f"{a['matrix'][lv].get(j, 0) or '.':>3}" for j in cols))
    if runtime_line:
        out += ["", paint(runtime_line, "dim")]
    out.append("")
    if cmp is None:
        out.append("No earlier calibration to compare with: this one is the baseline.")
        return "\n".join(out)
    out.append(f"Compared with {baseline} ({cmp['items']} items in both)")
    rows = [("overall", cmp["overall"])] + list(cmp["tags"].items())
    for name, t in rows:
        if not t:
            continue
        iv = t["interval"]
        chance = "beyond chance" if t["worse"] else "within chance" if (t["before"] or 0) > (t["now"] or 0) else ""
        mark = paint("✗ ", "red") if t["worse"] else "  "
        out.append(f"{mark}{name:<14} Spearman {_f(t['before'])} → {_f(t['now'])}"
                   + (f" ({t['now'] - t['before']:+.2f}{', ' + chance if chance else ''})"
                      if t["before"] is not None and t["now"] is not None else ""))
    far = [v for v in cmp["new_violations"] if v["gap"] >= 2]
    if far:
        out.append(paint(f"✗ {len(far)} new ordering violation{'s' * (len(far) != 1)} two or more apart:", "red"))
        out += [f"    {v['high']} (labeled {v['high_label']:g}) now judged below {v['low']} (labeled {v['low_label']:g})"
                for v in far[:5]]
    near = len(cmp["new_violations"]) - len(far)
    if near:
        out.append(paint(f"  {near} new near-miss{'es' * (near != 1)} one label apart (not failing)", "dim"))
    nv = cmp.get("new_variant_failures") or []
    if nv:
        out.append(paint(f"✗ {len(nv)} variant{'s' * (len(nv) != 1)} that behaved before no longer do:", "red"))
        out += [f"    {_variant(v)}" for v in nv[:5]]
    if cmp.get("cause"):
        out.append(paint(f"Why: {cmp['cause']}.", "yellow" if cmp.get("drift") else "dim"))
    out += ["", paint("Regressed.", "red") if cmp["regressed"] else paint("Calibrated as before.", "green")]
    return "\n".join(out)


def as_json(a: dict, cmp: Optional[dict]) -> dict:
    keep = {k: v for k, v in a.items() if k not in ("items",)}
    keep["consistency"] = {**a["consistency"], "widest": [r["id"] for r in a["consistency"]["widest"]]}
    keep["items"] = [{k: r.get(k) for k in ("id", "label", "judged", "scores", "spread", "flips", "tags")}
                     for r in a["items"]]
    return {"calibration": keep, "compared": cmp}


def new_id() -> str:
    return datetime.utcnow().strftime("c-%Y%m%d-%H%M%S-%f")[:-3]
