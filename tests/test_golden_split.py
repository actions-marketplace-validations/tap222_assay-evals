"""Golden splits (assay golden split): judges take examples from train, calibration reports on dev, and on
test only with --final; a judge that has seen the items it's measured on fails calibration."""
import json

from assay import calibrate
from assay.__main__ import main

from test_calibrate import project  # noqa: F401  the golden set and judge


def rows(root):
    return [json.loads(x) for x in (root / "golden.jsonl").read_text().splitlines() if x.strip()]


def test_split_stratified_kept_and_calibrated_on_dev(project, capsys):
    assert main(["golden", "split", "--train", "0.2", "--dev", "0.4"]) == 0
    first = {x["id"]: x["split"] for x in rows(project)}
    sizes = {s: sum(v == s for v in first.values()) for s in calibrate.SPLITS}
    assert sum(sizes.values()) == 30 and all(sizes.values()) and sizes["train"] < sizes["dev"]
    by_score = {}
    for x in calibrate.load_golden(project / "golden.jsonl"):
        by_score.setdefault(round(x["label"]), set()).add(x["split"])
    assert all(len(s) >= 2 for s in by_score.values())  # every label value is in more than one split
    assert main(["golden", "split"]) == 0 and {x["id"]: x["split"] for x in rows(project)} == first  # kept
    capsys.readouterr()
    assert main(["golden", "stats"]) == 0 and "train" in capsys.readouterr().out
    assert main(["calibrate", "--repeat", "1"]) == 0
    out = capsys.readouterr().out
    assert "the dev split" in out and f"{sizes['dev']}" in out
    assert main(["calibrate", "--repeat", "1", "--final"]) == 0 and "the test split" in capsys.readouterr().out


def test_a_judge_that_has_seen_dev_items_fails(project, capsys):
    main(["golden", "split"])
    items = rows(project)
    for x in items:  # outputs long enough to be recognizably one item's
        x["output"] += " because the order shipped on Tuesday"
    (project / "golden.jsonl").write_text("\n".join(json.dumps(x) for x in items) + "\n")
    dev = next(x for x in items if x["split"] == "dev")
    judges = project / "evals" / "judges.py"
    src = judges.read_text()
    judges.write_text(src + f"\nEXAMPLES = [{dev['output'].upper()!r}]  # pasted in as a few-shot example\n")
    capsys.readouterr()
    assert main(["calibrate", "--repeat", "1"]) == 1
    assert "leak" in capsys.readouterr().out.lower()
    judges.write_text(src.replace("def grade(", "from assay_sdk import golden_examples\n"
                                  "SHOTS = golden_examples('golden.jsonl', split='dev')\ndef grade(", 1))
    assert main(["calibrate", "--repeat", "1"]) == 1
    judges.write_text(src.replace("def grade(", "from assay_sdk import golden_examples\n"
                                  "SHOTS = golden_examples('golden.jsonl', k=3)\ndef grade(", 1))
    assert main(["calibrate", "--repeat", "1"]) == 0  # train examples: fine


def test_golden_examples_and_critiques(project):
    from assay_sdk import golden_examples
    main(["golden", "split"])
    assert main(["golden", "add", "extra-1", "--score", "2", "--by", "sam", "--output", "quality=2",
                 "--input", "q", "--critique", "Answers a different question."]) == 0
    x = next(x for x in calibrate.load_golden(project / "golden.jsonl") if x["id"] == "extra-1")
    assert x["critiques"] == ["Answers a different question."]
    shots = golden_examples(str(project / "golden.jsonl"), k=2)
    assert len(shots) == 2 and {"id", "input", "output", "label", "critique"} <= set(shots[0])
    train = {x["id"] for x in rows(project) if x.get("split") == "train"}
    assert {s["id"] for s in golden_examples(str(project / "golden.jsonl"))} == train
