"""Scoring document extraction: each field against its correct value, line items, rules.

    from assay_sdk.documents import score_document, Text, Money, Date, LineItems, total_of, before

    SCHEMA = {
        "invoice_number": Text(weight=3),
        "invoice_date": Date(day_first=True),
        "due_date": Date(day_first=True),
        "total": Money(tolerance=0.01, weight=3),
        "vendor.name": Text(),
        "line_items": LineItems({"description": Text(), "quantity": Number(), "amount": Money()},
                                key="description"),
    }
    RULES = [total_of("line_items.amount", equals="total"), before("invoice_date", "due_date")]

    def test_invoice_17(assay_case):
        extracted = my_pipeline("invoices/17.pdf", run=assay_case)
        score_document(assay_case, expected=LABELS["17"], extracted=extracted, schema=SCHEMA, rules=RULES)

Every field is a check of its own, and it says which way it went wrong:

  correct    the value matches (or both are empty: nothing there, nothing extracted)
  wrong      a value was extracted, and it isn't the right one
  missing    the document has a value, and nothing was extracted
  invented   nothing is there, and a value was extracted: usually the costliest error

Given the document's text (text=), a wrong or invented value also says how it was made up:

  format      the right value in the wrong shape: day and month swapped, a decimal separator
              read wrong, the same words spelled or ordered differently
  inferred    a guess from context: the value is in the document, just not as this field (the
              state named most often for governing law, the seller's name as the buyer)
  fabricated  the value is nowhere in the document

Format errors are told apart without the text; the other two need it.

Without a schema, every field either side has is scored, each by the type its correct value looks
like (a number, an amount, a date, else text), so a value only the extractor gave is invented, not
ignored, and "1,250.00" is 1250. With one, fields the extractor gave that it doesn't score are
named (`unscored`), so nothing disappears unsaid.

Zero is a value, not an empty one: 0 extracted where the document has nothing is invented, and
nothing extracted where it says 0 is missing, so the two errors stay apart.

"Matches" is per type: Text ignores case and spacing; Number and Money compare numbers with a
tolerance ("1.234,56 €" is 1234.56); Date reads the usual formats, with day_first for 03/04/2026.
A correct value that can't be read (a date that isn't one) is the label's problem: the check
couldn't be judged, and never counts against the extractor.

Line items are matched row to row whatever their order: the pairing of correct and extracted rows
with the most cells right overall (a maximum matching, as DocILE scores; by `key` when given), and
scored by row: a row is right when all its cells are. Rows missing, made up and duplicated are
counted apart; a table with none is complete. A Group (a party: name, address and role) is scored
as one unit, right only when all its parts are. `document` is one more check,
all fields correct, with the weighted share right as its score, and precision, recall and F1 over
its cells: each field is a cell, and so is each cell of the line items, one definition for both
(a cell in a missing row is missing, one in an invented row invented). Rules check the extracted values
against each other and need no correct values, so they run on production documents too
(check_rules). Each check carries its counts, from which `assay test` reports precision and
recall per field.
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import dataclass, field as dc_field
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Union

__all__ = ["Text", "Number", "Money", "Date", "LineItems", "score_document", "infer_schema", "check_rules", "total_of",
           "before", "required", "rule", "DocumentScore", "FieldScore", "EVALUATOR", "classify_document",
           "score_split", "SplitScore", "score_ocr", "OcrScore", "score_locations", "appears_in", "score_table",
           "TableScore", "teds", "Group", "FACETS", "anls", "correct_ocr", "rank_ocr", "OcrRanking",
           "OcrCorrectionError", "FORMAT", "INFERRED", "FABRICATED", "spot_check", "SPOT_CHECKS",
           "superseded_values", "SUPERSEDED"]

EVALUATOR = "assay.documents@1"
CORRECT, WRONG, MISSING, INVENTED = "correct", "wrong", "missing", "invented"
FORMAT, INFERRED, FABRICATED = "format", "inferred", "fabricated"
_MADE_UP = {FORMAT: "the right value in the wrong shape", INFERRED: "it's in the document, but not as this field",
            FABRICATED: "it's nowhere in the document"}


class Unreadable(ValueError):
    """A value that isn't what its type says (a date that isn't one)."""


def empty(v: Any) -> bool:
    return v is None or (isinstance(v, str) and not v.strip()) or (isinstance(v, (list, tuple, dict)) and not v)


# ---------- comparators ----------

class _Field:
    weight: float = 1.0

    def read(self, v: Any) -> Any:
        return v

    def same(self, a: Any, b: Any) -> bool:
        return a == b

    def show(self, v: Any) -> str:
        return str(v)

    def why(self, expected: Any, actual: Any) -> Optional[str]:
        """What the difference looks like, when it's a common one."""
        return None

    def reshaped(self, expected: Any, actual: Any) -> bool:
        """The right information in the wrong shape (both values as read)."""
        return False


class Text(_Field):
    """Text, ignoring case and spacing (and punctuation, with ignore_punctuation). exact=True: as is."""

    def __init__(self, exact: bool = False, ignore_punctuation: bool = False, weight: float = 1.0):
        self.exact, self.ignore_punctuation, self.weight = exact, ignore_punctuation, weight

    def read(self, v):
        s = str(v)
        if self.exact:
            return s
        s = re.sub(r"\s+", " ", s).strip().lower()
        return re.sub(r"[^\w\s]", "", s).strip() if self.ignore_punctuation else s

    def reshaped(self, expected, actual):  # "INV17" for "INV-17", "Doe, Jane" for "Jane Doe"
        e, a = str(expected).lower(), str(actual).lower()
        return re.sub(r"[\W_]+", "", e) == re.sub(r"[\W_]+", "", a) or \
            sorted(re.findall(r"\w+", e)) == sorted(re.findall(r"\w+", a))


_CURRENCY = re.compile(r"[$€£¥₹]|\b(usd|eur|gbp|jpy|inr|chf|cad|aud)\b", re.I)


def _number(v: Any, decimal_comma: Optional[bool]) -> Tuple[float, Optional[str]]:
    """(the number, its currency code or symbol if written). "1.234,56 €", "(12.00)", "USD 1,200"."""
    if isinstance(v, bool):
        raise Unreadable(f"{v!r} isn't a number")
    if isinstance(v, (int, float)):
        return float(v), None
    s = str(v).strip()
    cur = _CURRENCY.search(s)
    s = _CURRENCY.sub("", s).strip()
    neg = s.startswith("(") and s.endswith(")") or s.endswith("-") or s.startswith("-")
    s = s.strip("()-+ ").replace(" ", "").replace(" ", "").replace("'", "")
    if not re.fullmatch(r"[\d.,]+", s) or not re.search(r"\d", s):
        raise Unreadable(f"{v!r} isn't a number")
    if decimal_comma is None:  # the last separator is the decimal one when 1 or 2 digits follow it
        last = max(s.rfind(","), s.rfind("."))
        decimal_comma = last >= 0 and s[last] == "," and 0 < len(s) - last - 1 <= 2
        if s.count(".") > 1 and "," not in s:
            decimal_comma = True  # 1.234.567: the dots are thousands
    s = s.replace(".", "").replace(",", ".") if decimal_comma else s.replace(",", "")
    if s.count(".") > 1:
        raise Unreadable(f"{v!r} isn't a number")
    n = float(s)
    return (-n if neg else n), (cur.group(0).upper() if cur else None)


class Number(_Field):
    """A number within `tolerance` (absolute) or `relative` (a share of the correct value)."""

    def __init__(self, tolerance: float = 0.0, relative: float = 0.0, decimal_comma: Optional[bool] = None,
                 weight: float = 1.0):
        self.tolerance, self.relative, self.decimal_comma, self.weight = tolerance, relative, decimal_comma, weight

    def read(self, v):
        return _number(v, self.decimal_comma)[0]

    def same(self, a, b):
        return abs(a - b) <= max(self.tolerance, self.relative * abs(a)) + 1e-9

    def show(self, v):
        return f"{v:,.6g}" if abs(v) < 1e15 else str(v)

    def why(self, expected, actual):
        if expected and actual:
            for k in (10, 100, 1000):
                if abs(actual - expected * k) < 1e-6 * max(1, abs(expected * k)) or \
                        abs(actual * k - expected) < 1e-6 * max(1, abs(expected)):
                    return f"off by a factor of {k} (a decimal separator read wrong?)"
            if abs(actual + expected) < 1e-9:
                return "the sign is wrong"
        return None

    def reshaped(self, expected, actual):  # a decimal or thousands separator read the other way
        return bool(expected and actual) and any(
            abs(actual - expected * k) < 1e-6 * max(1, abs(expected * k)) or
            abs(actual * k - expected) < 1e-6 * max(1, abs(expected)) for k in (10, 100, 1000))


class Money(Number):
    """An amount: currency symbols and codes, thousands separators and "1.234,56" are read, and
    compared within `tolerance` (default half a cent). currency=True: the currency must match too,
    when both give one."""

    def __init__(self, tolerance: float = 0.005, currency: bool = False, decimal_comma: Optional[bool] = None,
                 weight: float = 1.0):
        super().__init__(tolerance=tolerance, decimal_comma=decimal_comma, weight=weight)
        self.currency = currency

    def read(self, v):
        n, cur = _number(v, self.decimal_comma)
        return (n, _SYMBOL.get(cur, cur)) if self.currency else n

    def same(self, a, b):
        if self.currency:
            (x, cx), (y, cy) = a, b
            return (cx is None or cy is None or cx == cy) and super().same(x, y)
        return super().same(a, b)

    def show(self, v):
        if self.currency:
            n, cur = v
            return f"{n:,.2f}" + (f" {cur}" if cur else "")
        return f"{v:,.2f}"

    def why(self, expected, actual):
        if self.currency:
            if expected[1] and actual[1] and expected[1] != actual[1]:
                return f"the currency is {actual[1]}, not {expected[1]}"
            return super().why(expected[0], actual[0])
        return super().why(expected, actual)

    def reshaped(self, expected, actual):
        if self.currency:
            if expected[1] and actual[1] and expected[1] != actual[1]:
                return False
            return super().reshaped(expected[0], actual[0])
        return super().reshaped(expected, actual)


_SYMBOL = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₹": "INR"}
_MONTHS = {m: i for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"], 1)}


class Date(_Field):
    """A date in any of the usual formats: 2026-03-04, 04.03.2026, 4 March 2026, March 4th, 2026,
    03/04/2026 (day_first says which; when one part is over 12 it decides itself)."""

    def __init__(self, day_first: bool = False, weight: float = 1.0):
        self.day_first, self.weight = day_first, weight

    def read(self, v):
        if isinstance(v, datetime):
            return v.date()
        if isinstance(v, date):
            return v
        s = re.sub(r"(\d)(st|nd|rd|th)\b", r"\1", str(v).strip().lower()).replace(",", " ")
        s = re.sub(r"\s+", " ", s)
        try:
            m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})(?:[t ].*)?", s)
            if m:
                return date(int(m[1]), int(m[2]), int(m[3]))
            m = re.fullmatch(r"(\d{1,2})\.(\d{1,2})\.(\d{2,4})", s)  # dots: day first, everywhere they're used
            if m:
                return date(_year(m[3]), int(m[2]), int(m[1]))
            m = re.fullmatch(r"(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})", s)
            if m:
                a, b, y = int(m[1]), int(m[2]), _year(m[3])
                day_first = True if a > 12 else False if b > 12 else self.day_first
                return date(y, b, a) if day_first else date(y, a, b)
            m = re.fullmatch(r"(\d{1,2}) ([a-z]{3})[a-z]*\.? (\d{4})", s)
            if m and m[2] in _MONTHS:
                return date(int(m[3]), _MONTHS[m[2]], int(m[1]))
            m = re.fullmatch(r"([a-z]{3})[a-z]*\.? (\d{1,2}) (\d{4})", s)
            if m and m[1] in _MONTHS:
                return date(int(m[3]), _MONTHS[m[1]], int(m[2]))
        except ValueError:
            pass
        raise Unreadable(f"{v!r} isn't a date")

    def show(self, v):
        return v.isoformat()

    def why(self, expected, actual):
        if expected.year == actual.year and expected.day == actual.month and expected.month == actual.day:
            return "day and month swapped"
        if expected.replace(year=actual.year) == actual:
            return "the year is wrong"
        return None

    def reshaped(self, expected, actual):  # 03/04 for 04/03
        return expected.year == actual.year and expected.day == actual.month and expected.month == actual.day \
            and expected != actual


def _year(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


class LineItems(_Field):
    """Rows of cells, matched row to row whatever their order: by `key` (a cell that names the
    row), else by the most cells in common. A row is right when every cell is."""

    def __init__(self, fields: Dict[str, _Field], key: Optional[str] = None, weight: float = 1.0):
        if key is not None and key not in fields:
            raise ValueError(f"LineItems: key {key!r} isn't one of its fields")
        self.fields, self.key, self.weight = fields, key, weight


class Group(_Field):
    """Fields that belong together, scored as one unit: a party's name, address and role. Each part
    is a check of its own (`grantor.role`); the group is right only when every part is. For several
    of them (the grantors), use LineItems: each row is a group."""

    def __init__(self, fields: Dict[str, _Field], weight: float = 1.0):
        self.fields, self.weight = fields, weight


# ---------- results ----------

@dataclass
class FieldScore:
    field: str
    kind: str  # correct | wrong | missing | invented | unreadable (the correct value couldn't be read)
    expected: Any = None
    actual: Any = None
    note: Optional[str] = None
    weight: float = 1.0
    counts: Dict[str, float] = dc_field(default_factory=dict)  # tp, fp, fn (and rows and cells, for line items)
    share: float = 1.0  # how much of it is right: 0 or 1, or the row F1 for line items
    part_of: Optional[str] = None  # a line-item column: the table it's part of, already counted there
    made_up: Optional[str] = None  # a wrong or invented value: format | inferred | fabricated (None: not told)
    grounded: bool = False  # scored with the document's text, so inferred and fabricated could be told apart
    grouped: bool = False  # a part of a Group, counted in it for accuracy, and as a value of its own
    critical: bool = False  # a field whose error stops straight-through processing (score_document critical=)

    @property
    def passed(self) -> Optional[bool]:
        return None if self.kind == "unreadable" else self.kind == CORRECT


@dataclass
class DocumentScore:
    fields: Dict[str, FieldScore]
    rules: Dict[str, Tuple[Optional[bool], str]]
    unscored: List[str] = dc_field(default_factory=list)  # extracted, with a value, but not in the schema

    @property
    def critical_correct(self) -> Optional[bool]:
        """Every critical field right: the document could go straight through. None: none declared."""
        crit = [f for f in self.fields.values() if f.critical]
        return all(f.passed is not False for f in crit) if crit else None

    @property
    def all_correct(self) -> bool:
        return all(f.passed is not False for f in self.fields.values())

    @property
    def accuracy(self) -> Optional[float]:
        """The weighted share of fields right (line items by their row F1)."""
        judged = [f for f in self.fields.values() if f.passed is not None and not f.part_of]
        w = sum(f.weight for f in judged)
        return sum(f.weight * f.share for f in judged) / w if w else None

    def wrong(self) -> List[FieldScore]:
        return [f for f in self.fields.values() if f.passed is False]

    @property
    def made_up(self) -> Dict[str, int]:
        """Wrong and invented values by how they were made up: format, inferred, fabricated."""
        out = {FORMAT: 0, INFERRED: 0, FABRICATED: 0}
        for f in self.fields.values():
            if f.made_up:
                out[f.made_up] += 1
        return out

    @property
    def cells(self) -> Dict[str, int]:
        """tp, fp, fn over cells: each field one, each line-item cell one (its table isn't counted
        again). Unweighted; a cell empty in both isn't counted."""
        tables = {f.part_of for f in self.fields.values() if f.part_of}
        out = {"tp": 0, "fp": 0, "fn": 0}
        for name, f in self.fields.items():
            if name not in tables:
                for k in out:
                    out[k] += int(f.counts.get(k) or 0)
        return out

    @property
    def precision(self) -> Optional[float]:
        """Of the cells extracted, the share right."""
        c = self.cells
        return c["tp"] / (c["tp"] + c["fp"]) if c["tp"] + c["fp"] else None

    @property
    def recall(self) -> Optional[float]:
        """Of the cells the document has, the share extracted right."""
        c = self.cells
        return c["tp"] / (c["tp"] + c["fn"]) if c["tp"] + c["fn"] else None

    @property
    def f1(self) -> float:
        """Cell F1: 1.0 when there's nothing to extract and nothing was."""
        c = self.cells
        return 2 * c["tp"] / (2 * c["tp"] + c["fp"] + c["fn"]) if c["tp"] + c["fp"] + c["fn"] else 1.0


def _get(doc: Any, path: str) -> Any:
    """doc["vendor"]["name"] for "vendor.name"; a key with a dot in it is tried first."""
    if isinstance(doc, dict) and path in doc:
        return doc[path]
    cur = doc
    for part in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(part)
        elif isinstance(cur, (list, tuple)) and part.isdigit() and int(part) < len(cur):
            cur = cur[int(part)]
        else:
            return getattr(cur, part, None) if cur is not None and not isinstance(cur, (str, int, float)) else None
    return cur


def _score_value(name: str, spec: _Field, exp: Any, act: Any) -> FieldScore:
    w = spec.weight
    if empty(exp) and empty(act):
        return FieldScore(name, CORRECT, exp, act, weight=w)
    if empty(exp):
        return FieldScore(name, INVENTED, exp, act, "a value the document doesn't have", w, {"fp": 1}, 0.0)
    try:
        e = spec.read(exp)
    except Unreadable as exc:
        return FieldScore(name, "unreadable", exp, act, f"the correct value can't be read: {exc}", w)
    if empty(act):
        return FieldScore(name, MISSING, exp, act, "nothing extracted", w, {"fn": 1}, 0.0)
    try:
        a = spec.read(act)
    except Unreadable as exc:
        return FieldScore(name, WRONG, exp, act, str(exc), w, {"fp": 1, "fn": 1}, 0.0)
    if spec.same(e, a):
        return FieldScore(name, CORRECT, exp, act, weight=w, counts={"tp": 1})
    why = spec.why(e, a)
    return FieldScore(name, WRONG, exp, act, f"{spec.show(a)}, not {spec.show(e)}" + (f": {why}" if why else ""),
                      w, {"fp": 1, "fn": 1}, 0.0)


def _made_up(spec: _Field, f: FieldScore, text: Optional[str]) -> Optional[str]:
    """How a wrong or invented value was made up; None when it wasn't, or can't be told."""
    if f.kind not in (WRONG, INVENTED) or empty(f.actual):
        return None
    if f.kind == WRONG:
        try:
            if spec.reshaped(spec.read(f.expected), spec.read(f.actual)):
                return FORMAT
        except Unreadable:
            pass
    if text is None:
        return None
    try:
        found = _found(spec, f.actual, text)
    except Unreadable:  # not a value of its type: look for it as it's written
        found = _found(Text(), f.actual, text)
    return INFERRED if found else FABRICATED


def _assign(w: List[List[float]]) -> List[Tuple[int, int]]:
    """The pairing of rows to columns with the most weight in all (the Hungarian method); pairs of
    weight 0 are left out. Ties go to rows kept in their place."""
    n, m = len(w), len(w[0]) if w else 0
    if not n or not m:
        return []
    flip = n > m
    if flip:
        w = [list(r) for r in zip(*w)]
        n, m = m, n
    big = max(max(r) for r in w) + 1
    cost = [[big - w[i][j] + 1e-9 * abs(i - j) for j in range(m)] for i in range(n)]
    u, v, p, way = [0.0] * (n + 1), [0.0] * (m + 1), [0] * (m + 1), [0] * (m + 1)
    for i in range(1, n + 1):
        p[0], j0 = i, 0
        minv, used = [float("inf")] * (m + 1), [False] * (m + 1)
        while True:
            used[j0] = True
            i0, delta, j1 = p[j0], float("inf"), 0
            for j in range(1, m + 1):
                if not used[j]:
                    c = cost[i0 - 1][j - 1] - u[i0] - v[j]
                    if c < minv[j]:
                        minv[j], way[j] = c, j0
                    if minv[j] < delta:
                        delta, j1 = minv[j], j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
    pairs = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j]]
    pairs = [(b, a) for a, b in pairs] if flip else pairs
    return sorted((a, b) for a, b in pairs if (w[b][a] if flip else w[a][b]) > 0)


def _duplicates(rows: List[Any], matched_rows: List[Any], same: Callable[[Any, Any], bool]) -> int:
    """Of the rows left unmatched, how many repeat a matched one."""
    return sum(1 for r in rows if any(same(r, m) for m in matched_rows))


def _cell_counts(spec: _Field, e: Any, a: Any) -> Dict[str, int]:
    """One cell, counted as a field is: wrong is a false positive and a false negative."""
    if empty(e) and empty(a):
        return {}
    if empty(e):
        return {"fp": 1}
    try:
        x = spec.read(e)
    except Unreadable:
        return {}  # the label's problem: not judged
    if empty(a):
        return {"fn": 1}
    try:
        return {"tp": 1} if spec.same(x, spec.read(a)) else {"fp": 1, "fn": 1}
    except Unreadable:
        return {"fp": 1, "fn": 1}


def _cell_ok(spec: _Field, e: Any, a: Any) -> bool:
    if empty(e) and empty(a):
        return True
    if empty(e) or empty(a):
        return False
    try:
        return spec.same(spec.read(e), spec.read(a))
    except Unreadable:
        return False


def _score_rows(name: str, spec: LineItems, exp: Any, act: Any) -> Tuple[FieldScore, Dict[str, FieldScore]]:
    exp, act = list(exp or []), list(act or [])
    cols = list(spec.fields)
    same = {(i, j): sum(_cell_ok(spec.fields[c], _get(e, c), _get(a, c)) for c in cols)
            for i, e in enumerate(exp) for j, a in enumerate(act)}
    if spec.key:  # a row pairs only with one of the same key; +1 so a key match with no other cell counts
        k = spec.fields[spec.key]
        allowed = lambda i, j: _cell_ok(k, _get(exp[i], spec.key), _get(act[j], spec.key))
    else:
        allowed = lambda i, j: same[(i, j)] > 0
    matched = _assign([[same[(i, j)] + 1 if allowed(i, j) else 0 for j in range(len(act))]
                       for i in range(len(exp))])
    used_e, used_a = {i for i, _ in matched}, {j for _, j in matched}
    right = sum(1 for i, j in matched if same[(i, j)] == len(cols))
    tp, fp, fn = right, len(act) - right, len(exp) - right
    f1 = 2 * tp / (2 * tp + fp + fn) if (tp + fp + fn) else 1.0
    lost, extra = len(exp) - len(used_e), len(act) - len(used_a)
    dup = _duplicates([act[j] for j in range(len(act)) if j not in used_a], [act[j] for j in used_a],
                      lambda r, m: all(_cell_ok(spec.fields[c], _get(m, c), _get(r, c)) for c in cols))
    notes = []
    if lost:
        notes.append(f"{lost} row(s) missing")
    if extra - dup:
        notes.append(f"{extra - dup} row(s) invented")
    if dup:
        notes.append(f"{dup} row(s) duplicated")
    per_col: Dict[str, FieldScore] = {}
    for c in cols:
        ok = sum(1 for i, j in matched if _cell_ok(spec.fields[c], _get(exp[i], c), _get(act[j], c)))
        bad = [(i, j) for i, j in matched if not _cell_ok(spec.fields[c], _get(exp[i], c), _get(act[j], c))]
        if bad:
            i, j = bad[0]
            label = _get(exp[i], spec.key) if spec.key else f"row {i + 1}"
            notes.append(f"{c} wrong in {len(bad)} row(s), e.g. {label}: {_get(act[j], c)!r}, not {_get(exp[i], c)!r}")
        n = len(exp)
        cells = {"tp": 0, "fp": 0, "fn": 0}
        for i, j in matched:
            for k, v in _cell_counts(spec.fields[c], _get(exp[i], c), _get(act[j], c)).items():
                cells[k] += v
        cells["fn"] += sum(not empty(_get(exp[i], c)) for i in range(len(exp)) if i not in used_e)
        cells["fp"] += sum(not empty(_get(act[j], c)) for j in range(len(act)) if j not in used_a)
        per_col[f"{name}.{c}"] = FieldScore(
            f"{name}.{c}", CORRECT if ok == n and not (len(act) - len(used_a)) else WRONG,
            n, ok, f"{ok} of {n} right" if ok < n else f"{len(act) - len(used_a)} row(s) invented"
            if len(act) - len(used_a) else None, spec.weight,
            cells, ok / n if n else 1.0, part_of=name)
    whole = FieldScore(name, CORRECT if tp == len(exp) == len(act) else (MISSING if not act else WRONG),
                       len(exp), len(act), "; ".join(notes) or None, spec.weight,
                       {"tp": tp, "fp": fp, "fn": fn, "rows": len(exp), "rows_extracted": len(act),
                        **({"rows_missing": lost} if lost else {}),
                        **({"rows_invented": extra - dup} if extra - dup else {}),
                        **({"rows_duplicated": dup} if dup else {})}, f1)
    return whole, per_col


def _score_group(name: str, spec: Group, exp: Any, act: Any, text: Optional[str]
                 ) -> Tuple[FieldScore, Dict[str, FieldScore]]:
    parts: Dict[str, FieldScore] = {}
    for c, sub in spec.fields.items():
        f = _score_value(f"{name}.{c}", sub, _get(exp, c) if exp is not None else None,
                         _get(act, c) if act is not None else None)
        f.part_of, f.grouped, f.weight = name, True, spec.weight
        f.made_up, f.grounded = _made_up(sub, f, text), text is not None
        if f.made_up:
            f.note = f"{f.note}; {f.made_up}: {_MADE_UP[f.made_up]}"
        parts[f.field] = f
    has_e = any(not empty(p.expected) for p in parts.values())
    has_a = any(not empty(p.actual) for p in parts.values())
    bad = [p.field.split(".")[-1] for p in parts.values() if p.passed is False]
    n = {"members": len(parts)}
    if not bad:
        whole = FieldScore(name, CORRECT, exp, act, weight=spec.weight, counts={"tp": 1, **n} if has_e else n)
    elif not has_e:
        whole = FieldScore(name, INVENTED, exp, act, "a group the document doesn't have", spec.weight,
                           {"fp": 1, **n}, 0.0)
    elif not has_a:
        whole = FieldScore(name, MISSING, exp, act, "nothing extracted", spec.weight, {"fn": 1, **n}, 0.0)
    else:
        whole = FieldScore(name, WRONG, exp, act, f"{', '.join(bad)} wrong ({len(parts) - len(bad)} of "
                           f"{len(parts)} right)", spec.weight, {"fp": 1, "fn": 1, **n}, 0.0)
    return whole, parts


# ---------- rules: the extracted values against each other ----------

@dataclass
class Rule:
    name: str
    fn: Callable[[Any], Tuple[Optional[bool], str]]  # (True/False, why); None: can't be checked here


def rule(name: str, fn: Callable[[Any], Any]) -> Rule:
    """A rule of your own: fn(extracted) returns True/False, or (True/False, why); None skips it."""
    def run(doc):
        out = fn(doc)
        return out if isinstance(out, tuple) else (out, "")
    return Rule(name, run)


def _sum(doc: Any, path: str, spec: Number) -> Optional[float]:
    """A field's value, or with rows.cell the sum over the rows; None when any is missing."""
    if "." in path and isinstance(_get(doc, path.split(".", 1)[0]), (list, tuple)):
        rows_path, cell = path.split(".", 1)
        vals = [_get(r, cell) for r in _get(doc, rows_path)]
    else:
        vals = [_get(doc, path)]
    if not vals or any(empty(v) for v in vals):
        return None
    try:
        return sum(_number(v, spec.decimal_comma)[0] for v in vals)
    except Unreadable:
        return None


def total_of(parts: Union[str, Sequence[str]], equals: str, tolerance: float = 0.01,
             decimal_comma: Optional[bool] = None) -> Rule:
    """The parts add up to the total: total_of("line_items.amount", equals="total"), or
    total_of(["subtotal", "tax"], equals="total"). Skipped when a value is missing."""
    parts = [parts] if isinstance(parts, str) else list(parts)
    spec = Number(decimal_comma=decimal_comma)

    def check(doc):
        got = [_sum(doc, p, spec) for p in parts]
        want = _sum(doc, equals, spec)
        if want is None or any(g is None for g in got):
            return None, ""
        s = sum(got)
        return abs(s - want) <= tolerance + 1e-9, f"{' + '.join(parts)} = {s:,.2f}, {equals} = {want:,.2f}"
    return Rule(f"{equals} = {' + '.join(parts)}", check)


def before(first: str, then: str, day_first: bool = False, same_day: bool = True) -> Rule:
    """One date is on or before another: before("invoice_date", "due_date")."""
    spec = Date(day_first=day_first)

    def check(doc):
        a, b = _get(doc, first), _get(doc, then)
        if empty(a) or empty(b):
            return None, ""
        try:
            x, y = spec.read(a), spec.read(b)
        except Unreadable:
            return None, ""
        return (x <= y if same_day else x < y), f"{first} {x.isoformat()}, {then} {y.isoformat()}"
    return Rule(f"{first} before {then}", check)


def required(*fields: str) -> Rule:
    """These fields have a value."""
    def check(doc):
        gone = [f for f in fields if empty(_get(doc, f))]
        return not gone, f"no {', '.join(gone)}" if gone else ""
    return Rule(f"has {', '.join(fields)}", check)


def _rules(rules: Sequence[Rule], extracted: Any) -> Dict[str, Tuple[Optional[bool], str]]:
    out = {}
    for r in rules:
        try:
            out[r.name] = r.fn(extracted)
        except Exception as exc:  # a rule that breaks is a bug in the rule, not a failed document
            out[r.name] = (None, f"the rule failed: {type(exc).__name__}: {exc}")
    return out


# ---------- recording ----------

FACETS = {  # what robustness is sliced by; any other key is kept too
    "source": "digital or scanned",
    "quality": "clean, skewed, noisy, low-resolution, ...",
    "stamps": "a stamp or seal over the text (yes / no)",
    "handwriting": "handwritten values or notes (yes / no)",
    "language": "the document's language, e.g. de",
    "currency": "the amounts' currency, e.g. EUR",
    "template": "the supplier's layout, e.g. acme-v3",
    "template_seen": "whether the model was built or tuned on that layout (seen / unseen)",
}


def _facets(facets: Optional[Dict[str, Any]]) -> Dict[str, str]:
    """Facet values as text: True/False as yes/no, and for template_seen, seen/unseen."""
    out = {}
    for k, v in (facets or {}).items():
        if v is None or v == "":
            continue
        if not isinstance(k, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", k):
            raise ValueError(f"score_document: facet {k!r}: lowercase letters, digits and _, e.g. template_seen")
        if isinstance(v, bool):
            v = ("seen" if v else "unseen") if k == "template_seen" else ("yes" if v else "no")
        out[k] = str(v)[:64]
    return out


def _record(run, name: str, f: FieldScore, confidence: Optional[float] = None,
            facets: Optional[Dict[str, str]] = None) -> None:
    raw = json.dumps({"kind": f.kind, "weight": f.weight, "share": round(f.share, 6), **f.counts,
                      **({"part_of": f.part_of} if f.part_of else {}),
                      **({"made_up": f.made_up} if f.made_up else {}), **({"grounded": True} if f.grounded else {}),
                      **({"grouped": True} if f.grouped else {}), **({"critical": True} if f.critical else {}),
                      **({"confidence": float(confidence)} if confidence is not None else {}),
                      **({"facets": facets} if facets else {})})
    if f.kind == "unreadable":
        run.check(name, "error", expected=f.expected, actual=f.actual, evaluator=EVALUATOR, reason=f.note,
                  error_kind="invalid", raw_output=raw)
        return
    # No score: a comparison, not a judge (a scored check is listed with the judges, for calibration).
    run.check(name, "pass" if f.passed else "fail", expected=f.expected, actual=f.actual, evaluator=EVALUATOR,
              reason=None if f.passed else f"{f.kind}: {f.note}" if f.note else f.kind,
              category=None if f.passed else f.kind, raw_output=raw)


def _record_rules(run, results: Dict[str, Tuple[Optional[bool], str]]) -> None:
    for name, (ok, why) in results.items():
        if ok is None and not why.startswith("the rule failed"):
            continue  # not checkable on this document (a value it needs is missing)
        run.check(f"rule: {name}", "error" if ok is None else "pass" if ok else "fail", evaluator=EVALUATOR,
                  reason=None if ok else why, error_kind="error" if ok is None else None,
                  raw_output=json.dumps({"kind": "rule"}))


def score_document(run, expected: Any, extracted: Any, schema: Optional[Dict[str, _Field]] = None,
                   rules: Sequence[Rule] = (), confidence: Optional[Dict[str, float]] = None,
                   text: Optional[str] = None, critical: Sequence[str] = (),
                   facets: Optional[Dict[str, Any]] = None) -> DocumentScore:
    """Score one document's extraction against its correct values, and record each field, the line
    items, `document` (all fields correct) and each rule as checks on `run` (None: only score).

    schema: how each field is compared. None: every field either side has, typed by its values
    (infer_schema). Given, fields extracted outside it are listed in `unscored`.

    confidence: the extractor's confidence per field (0-1), where it gives one. Recorded with each
    field, so the report can say whether a confident value is a right one, which threshold would
    auto-approve safely, and how many wrong values a threshold lets through.

    text: the document's text (its OCR). With it, each wrong or invented value says whether it was
    inferred (in the document, not as this field) or fabricated (nowhere in it); format errors
    (the right value in the wrong shape) are told without it. Line-item cells aren't sorted.

    critical: the fields whose error stops straight-through processing (a tax number, the total).
    The document check then also says whether every one of them was right, and the report and the
    dashboard give their accuracy apart from the rest.

    facets: what the document is like, to slice robustness by (FACETS): source (digital or
    scanned), quality, stamps, handwriting, language, currency, template, and template_seen (False:
    a layout the model was never built or tuned on, where regressions tend to hide). Recorded with
    every check; the report compares each slice with the baseline's."""
    unscored = [] if schema is None else _unscored(extracted, schema)
    if schema is None:
        schema = infer_schema(expected, extracted)
    fields: Dict[str, FieldScore] = {}
    for name, spec in schema.items():
        e, a = _get(expected, name), _get(extracted, name)
        if isinstance(spec, LineItems):
            whole, cols = _score_rows(name, spec, e, a)
            fields[name] = whole
            fields.update(cols)
        elif isinstance(spec, Group):
            whole, parts = _score_group(name, spec, e, a, text)
            fields[name] = whole
            fields.update(parts)
        else:
            f = _score_value(name, spec, e, a)
            f.made_up, f.grounded = _made_up(spec, f, text), text is not None
            if f.made_up:
                f.note = f"{f.note}; {f.made_up}: {_MADE_UP[f.made_up]}"
            fields[name] = f
    facets = _facets(facets)
    unknown = [c for c in critical if c not in fields]
    if unknown:
        raise ValueError(f"score_document: critical {', '.join(unknown)} isn't in the schema")
    for c in critical:
        fields[c].critical = True
    doc = DocumentScore(fields, _rules(rules, extracted), unscored)
    if run is not None:
        for name, f in fields.items():
            _record(run, name, f, (confidence or {}).get(name) if not f.part_of else None, facets)
        bad = doc.wrong()
        acc = doc.accuracy
        run.check("document", "pass" if doc.all_correct else "fail", evaluator=EVALUATOR,
                  reason=None if doc.all_correct else "wrong: " + ", ".join(f"{f.field} ({f.kind})" for f in bad[:6]),
                  raw_output=json.dumps({"kind": "document", "accuracy": acc, "fields": len(fields),
                                         "wrong": len(bad), "cells": doc.cells, "f1": round(doc.f1, 6),
                                         **({"unscored": unscored} if unscored else {}),
                                         **({"critical_correct": doc.critical_correct} if critical else {}),
                                         **({"facets": facets} if facets else {})}))
        _record_rules(run, doc.rules)
    return doc


def _guess(v: Any) -> _Field:
    """The type a value looks like: a date, an amount (with a currency), a number, else text. A
    string of digits with a leading zero ("00123", a zip code) is an identifier: text."""
    if isinstance(v, bool):
        return Text()
    if isinstance(v, (int, float)):
        return Number()
    if isinstance(v, (date, datetime)):
        return Date()
    s = str(v).strip()
    try:
        Date().read(s)
        return Date()
    except Unreadable:
        pass
    if re.fullmatch(r"0\d+", s):
        return Text()
    try:
        _, cur = _number(s, None)
    except (Unreadable, ValueError):
        return Text()
    return Money() if cur else Number()


def _keys(d: Any) -> List[str]:
    return list(d) if isinstance(d, dict) else []


def infer_schema(expected: Any, extracted: Any = None, prefix: str = "") -> Dict[str, _Field]:
    """A schema from the values themselves: every field either side has, typed by its correct value
    (by the extracted one where there's none). Nested objects become dotted fields
    ("vendor.name"), lists of objects line items."""
    out: Dict[str, _Field] = {}
    for k in dict.fromkeys(_keys(expected) + _keys(extracted)):
        e = expected.get(k) if isinstance(expected, dict) else None
        a = extracted.get(k) if isinstance(extracted, dict) else None
        v = a if empty(e) else e
        if any(isinstance(x, (list, tuple)) and x and all(isinstance(r, dict) for r in x) for x in (e, a)):
            rows = [r for x in (e, a) if isinstance(x, (list, tuple)) for r in x if isinstance(r, dict)]
            cols: Dict[str, _Field] = {}
            for c in dict.fromkeys(c for r in rows for c in r):
                sample = next((r[c] for r in rows if not empty(r.get(c))), None)
                cols[c] = _guess(sample) if sample is not None else Text()
            out[prefix + k] = LineItems(cols)
        elif isinstance(e, dict) or isinstance(a, dict):
            out.update(infer_schema(e if isinstance(e, dict) else {}, a if isinstance(a, dict) else {},
                                    f"{prefix}{k}."))
        else:
            out[prefix + k] = _guess(v) if not empty(v) else Text()
    return out


def _unscored(extracted: Any, schema: Dict[str, _Field]) -> List[str]:
    """Extracted fields with a value that no schema field covers ("vendor" is covered by "vendor.name")."""
    out = []
    for k in _keys(extracted):
        if not empty(extracted[k]) and not any(n == k or n.startswith(k + ".") or k.startswith(n + ".")
                                               for n in schema):
            out.append(k)
    return out


def check_rules(run, extracted: Any, rules: Sequence[Rule]) -> Dict[str, Tuple[Optional[bool], str]]:
    """Only the rules, on extracted values without correct ones (a production document)."""
    out = _rules(rules, extracted)
    if run is not None:
        _record_rules(run, out)
    return out


# ---------- document type ----------

def classify_document(run, expected: Any, predicted: Any, confidence: Optional[float] = None) -> bool:
    """Whether the document's type was classified right ("invoice" vs "Invoice " is the same type),
    recorded as the check `document_type`. The report counts every run's pairs into a confusion
    matrix, with precision and recall per type."""
    e, p = Text().read(expected) if not empty(expected) else None, Text().read(predicted) if not empty(predicted) else None
    if e is None:
        if run is not None:
            run.check("document_type", "error", expected=expected, actual=predicted, evaluator=EVALUATOR,
                      reason="no correct type to compare with", error_kind="invalid")
        return False
    ok = e == p
    if run is not None:
        run.check("document_type", "pass" if ok else "fail", expected=expected, actual=predicted, evaluator=EVALUATOR,
                  reason=None if ok else f"classified as {predicted!r}, not {expected!r}" if p else "no type given",
                  category=None if ok else "misclassified",
                  raw_output=json.dumps({"kind": "classification", "expected": e, "predicted": p,
                                         **({"confidence": float(confidence)} if confidence is not None else {})}))
    return ok


# ---------- splitting a file into its documents ----------

@dataclass
class SplitScore:
    expected: List[Tuple[int, int]]
    predicted: List[Tuple[int, int]]
    right: List[Tuple[int, int]]  # documents split exactly: the same first and last page
    notes: List[str]
    boundaries: Dict[str, int]  # tp, fp, fn over the pages a new document starts on (after the first)
    panoptic: Dict[str, float] = dc_field(default_factory=dict)  # iou (summed over matches), tp, fp, fn
    drags: int = 0  # pages a reviewer must move to put the split right (minimum drags and drops)
    pages: int = 0

    @property
    def correct(self) -> bool:
        return self.expected == self.predicted

    @property
    def pq(self) -> float:
        """Panoptic quality: documents matched when they share over half their pages (IoU > 0.5),
        their mean IoU (sq) times the F1 of matching (rq)."""
        p = self.panoptic
        den = p["tp"] + 0.5 * p["fp"] + 0.5 * p["fn"]
        return p["iou"] / den if den else 1.0

    @property
    def sq(self) -> float:
        return self.panoptic["iou"] / self.panoptic["tp"] if self.panoptic["tp"] else 0.0

    @property
    def rq(self) -> float:
        p = self.panoptic
        den = p["tp"] + 0.5 * p["fp"] + 0.5 * p["fn"]
        return p["tp"] / den if den else 1.0


def _segments(v: Any, page_count: Optional[int]) -> List[Tuple[int, int]]:
    """Documents as (first page, last page), 1-based: from ranges, page lists, or the first pages."""
    items = list(v or [])
    if items and all(isinstance(x, int) for x in items):  # first pages: each runs to the next one
        if page_count is None:
            raise ValueError("score_split: first pages alone need page_count")
        starts = sorted(set(items))
        return [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else page_count) for i, s in enumerate(starts)]
    out = []
    for x in items:
        if isinstance(x, dict):
            x = x.get("pages") or (x.get("start"), x.get("end"))
        x = list(x)
        out.append((int(min(x)), int(max(x))) if len(x) != 2 else (int(x[0]), int(x[1])))
    return sorted(out)


def _pages(s: Tuple[int, int]) -> str:
    return f"page {s[0]}" if s[0] == s[1] else f"pages {s[0]}-{s[1]}"


def _panoptic(exp: List[Tuple[int, int]], pred: List[Tuple[int, int]]) -> Dict[str, float]:
    sets = lambda segs: [set(range(a, b + 1)) for a, b in segs]
    es, ps = sets(exp), sets(pred)
    iou, tp = 0.0, 0
    for e in es:  # over half their pages in common: at most one match each, no assignment needed
        for p in ps:
            x = len(e & p) / len(e | p)
            if x > 0.5:
                iou, tp = iou + x, tp + 1
    return {"iou": iou, "tp": tp, "fp": len(ps) - tp, "fn": len(es) - tp}


def _drags(exp: List[Tuple[int, int]], pred: List[Tuple[int, int]]) -> Tuple[int, int]:
    """(pages to move, pages): each correct document kept as the predicted one it shares most with,
    one to one, for the most pages kept in all; every other page is dragged once, to its document
    (or to a new one). Pages the prediction left out are dragged in."""
    es = [set(range(a, b + 1)) for a, b in exp]
    ps = [set(range(a, b + 1)) for a, b in pred]
    kept = _assign([[len(e & p) for p in ps] for e in es])
    pages = len(set().union(*es)) if es else 0
    return pages - sum(len(es[i] & ps[j]) for i, j in kept), pages


def score_split(run, expected: Any, predicted: Any, page_count: Optional[int] = None) -> SplitScore:
    """Score how a file was split into documents, recorded as the check `split`: passes when every
    document starts and ends on the right page. Documents are given as page ranges ((1, 2), (3, 3)),
    page lists, or their first pages (with page_count). Says what went wrong: documents merged,
    one cut in two, a boundary a page off.

    Scored three ways: pages where a new document starts (precision and recall), documents split
    exactly, and panoptic quality (pq: documents matched on over half their pages, weighted by how
    much they overlap), the metric found most fitting for page stream segmentation. And `drags`,
    the fewest pages a reviewer must drag to put it right: what a split error costs in human time."""
    exp, pred = _segments(expected, page_count), _segments(predicted, page_count)
    right = sorted(set(exp) & set(pred))
    starts = lambda segs: {s[0] for s in segs} - {min((x[0] for x in segs), default=1)}
    es, ps = starts(exp), starts(pred)
    missed, extra = sorted(es - ps), sorted(ps - es)
    notes, explained = [], set()
    for m in missed:  # a boundary a page or two off: one note, not a merge and a cut
        near = min((x for x in extra if abs(x - m) <= 2 and x not in explained), key=lambda x: abs(x - m),
                   default=None)
        if near is not None:
            notes.append(f"the document starting on page {m} was split at page {near}")
            explained |= {m, near}
    for e in exp:
        if e in right or e[0] in explained or e[1] + 1 in explained:
            continue
        over = [p for p in pred if p[0] <= e[1] and p[1] >= e[0]]
        if len(over) == 1 and over[0][0] <= e[0] and over[0][1] >= e[1] and over[0] != e:
            others = [x for x in exp if x != e and over[0][0] <= x[0] and x[1] <= over[0][1]]
            if others:
                note = f"{_pages(over[0])} came out as one document, which is {len(others) + 1}"
                if note not in notes:
                    notes.append(note)
                continue
        if len(over) > 1 and all(e[0] <= p[0] and p[1] <= e[1] for p in over):
            notes.append(f"{_pages(e)} is one document, cut into {len(over)}")
        else:
            notes.append(f"{_pages(e)}: " + (", ".join(_pages(p) for p in over) if over else "no document") + " instead")
    bounds = {"tp": len(es & ps), "fp": len(ps - es), "fn": len(es - ps)}
    drags, pages = _drags(exp, pred)
    score = SplitScore(exp, pred, right, notes, bounds, _panoptic(exp, pred), drags, pages)
    if drags:
        notes.append(f"{drags} page{'s' * (drags != 1)} to move by hand")
    if run is not None:
        run.check("split", "pass" if score.correct else "fail", expected=json.dumps(exp), actual=json.dumps(pred),
                  evaluator=EVALUATOR, reason=None if score.correct else "; ".join(notes[:4]),
                  category=None if score.correct else "split_wrong",
                  raw_output=json.dumps({"kind": "split", "documents": len(exp), "tp": len(right),
                                         "fp": len(pred) - len(right), "fn": len(exp) - len(right),
                                         "boundaries": bounds, "panoptic": score.panoptic,
                                         "pq": round(score.pq, 6), "drags": drags, "pages": pages}))
    return score


# ---------- OCR: the text read from a page against what it says ----------

def _edits(a: Sequence, b: Sequence) -> int:
    """Levenshtein distance: insertions, deletions and substitutions turning a into b."""
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        cur = [i]
        for j, y in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (x != y)))
        prev = cur
    return prev[-1]


def _changed_lines(ref: str, hyp: str) -> List[Tuple[str, str]]:
    """The stretches of lines that differ between two texts, aligned line by line (a page is
    thousands of characters: whole-page Levenshtein in Python takes seconds; lines that match cost
    nothing)."""
    import difflib
    a, b = ref.split("\n"), hyp.split("\n")
    out = []
    for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if op == "equal":
            continue
        if op == "replace" and i2 - i1 == j2 - j1:  # as many lines read as there are: line for line
            out += [(x, y) for x, y in zip(a[i1:i2], b[j1:j2]) if x != y]
        else:
            out.append(("\n".join(a[i1:i2]), "\n".join(b[j1:j2])))
    return out


def _confusions(lines: List[Tuple[str, str]], max_len: int = 2000) -> Tuple[Dict[Tuple[str, str], int],
                                                                          Dict[Tuple[str, str], int]]:
    """What was read as what, from the lines that differ: characters ("l" read as "i", "rn" as "m",
    one lost: "l" as ""), and words ("Total" as "Tota1"). Aligned with difflib, stretch by
    stretch; a line longer than max_len is skipped, not guessed at."""
    import difflib
    chars: Dict[Tuple[str, str], int] = defaultdict(int)
    words: Dict[Tuple[str, str], int] = defaultdict(int)
    for x, y in lines:
        for a, b in zip(x.split("\n"), y.split("\n")) if x.count("\n") == y.count("\n") else [(x, y)]:
            if len(a) > max_len or len(b) > max_len:
                continue
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
                if op == "equal":
                    continue
                e, r = a[i1:i2], b[j1:j2]
                if op == "replace" and len(e) == len(r):
                    for p, q in zip(e, r):
                        if p != q:
                            chars[(p, q)] += 1
                elif len(e) <= 3 and len(r) <= 3:  # "rn" as "m", "l" lost: a confusion, not a rewrite
                    chars[(e.strip(), r.strip())] += 1 if e.strip() or r.strip() else 0
            aw, bw = a.split(), b.split()
            for op, i1, i2, j1, j2 in difflib.SequenceMatcher(None, aw, bw, autojunk=False).get_opcodes():
                if op == "replace" and i2 - i1 == j2 - j1:
                    for p, q in zip(aw[i1:i2], bw[j1:j2]):
                        words[(p, q)] += 1
    return {k: v for k, v in chars.items() if v and k != ("", "")}, dict(words)


def _top(counts: Dict[Tuple[str, str], int], n: int) -> List[list]:
    return [[a, b, c] for (a, b), c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:n]]


def _bounded(a: Sequence, b: Sequence) -> int:
    """Levenshtein, or when both are huge (a page read as something else entirely), the longer length."""
    return _edits(a, b) if len(a) * len(b) <= 4_000_000 else max(len(a), len(b))


def _reading_order(ref: List[str], hyp: List[str]) -> Tuple[Optional[float], int]:
    """Whether the lines were read in the page's order, apart from whether they were read right.
    Each line read is matched to the page's line it is (the same text, else the most alike, if
    it's at least 60% alike); the order score is the share of matched lines in the longest run
    that keeps the page's order. Also the character edits with the lines put back in order: what
    the reading got wrong, order aside."""
    import bisect
    import difflib
    free = defaultdict(list)
    for i, line in enumerate(ref):
        free[line].append(i)
    match, left = {}, []
    for j, line in enumerate(hyp):
        if free.get(line):
            match[j] = free[line].pop(0)
        else:
            left.append(j)
    unused = sorted(i for ids in free.values() for i in ids)
    if left and unused and len(left) * len(unused) <= 40_000:
        pairs = []
        for j in left:
            for i in unused:
                m = difflib.SequenceMatcher(None, ref[i], hyp[j], autojunk=False)
                if m.real_quick_ratio() >= 0.6 and m.quick_ratio() >= 0.6:
                    r = m.ratio()
                    if r >= 0.6:
                        pairs.append((r, j, i))
        taken_i, taken_j = set(), set()
        for r, j, i in sorted(pairs, reverse=True):
            if i not in taken_i and j not in taken_j:
                match[j] = i
                taken_i.add(i)
                taken_j.add(j)
    elif left and unused:  # far too many to compare: pair them by position
        for j, i in zip(left, unused):
            match[j] = i
    order = [match[j] for j in sorted(match)]
    tails: List[int] = []  # longest increasing run of page positions, in reading order
    for x in order:
        k = bisect.bisect_left(tails, x)
        tails[k:k + 1] = [x]
    score = len(tails) / len(order) if order else None
    edits = sum(_bounded(ref[i], hyp[j]) for j, i in match.items())
    edits += sum(len(ref[i]) for i in set(range(len(ref))) - set(match.values()))
    edits += sum(len(hyp[j]) for j in set(range(len(hyp))) - set(match))
    return score, edits


@dataclass
class OcrScore:
    chars: int
    char_errors: int
    words: int
    word_errors: int
    digits: int
    digit_errors: int
    lines: List[Tuple[str, str]]  # (what the page says, what was read), where they differ
    order: Optional[float] = None  # the share of lines read in the page's order
    order_free_errors: int = 0  # character edits with the lines put back in the page's order
    letters: int = 0
    letter_errors: int = 0
    confusions: Dict[Tuple[str, str], int] = dc_field(default_factory=dict)  # (on the page, read as): times
    word_confusions: Dict[Tuple[str, str], int] = dc_field(default_factory=dict)

    @property
    def letter_error_rate(self) -> Optional[float]:
        return self.letter_errors / self.letters if self.letters else None

    @property
    def order_free_cer(self) -> float:
        return self.order_free_errors / self.chars if self.chars else 0.0

    @property
    def cer(self) -> float:
        return self.char_errors / self.chars if self.chars else (0.0 if not self.char_errors else 1.0)

    @property
    def wer(self) -> float:
        return self.word_errors / self.words if self.words else (0.0 if not self.word_errors else 1.0)

    @property
    def digit_error_rate(self) -> Optional[float]:
        return self.digit_errors / self.digits if self.digits else None


def score_ocr(run, expected: str, read: str, page: Optional[int] = None, max_cer: float = 0.05,
              max_digit_errors: Optional[int] = None, case: bool = True, min_order: Optional[float] = None) -> OcrScore:
    """Score OCR text against what the page says, recorded as the check `ocr` (`ocr page 3` with
    `page`): the character error rate (edits over the page's characters), the word error rate, and
    the digits on their own, since a wrong digit is a wrong amount, and the letters on their own. What
    was read as what is kept too: characters ("l" as "i", "rn" as "m") and words, so the report can
    say which confusions a new version brought or fixed. Spacing doesn't count; case
    does unless case=False. It fails over `max_cer`, or with more than `max_digit_errors` wrong
    digits when that's given.

    Reading order is scored apart: the share of lines read in the page's order (a two-column page
    read across the columns scores low), and the character error rate with the lines put back in
    order, so text read right in the wrong order isn't counted as text read wrong. `min_order`
    fails a page read out of order."""
    norm = lambda t: "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in str(t or "").splitlines() if ln.strip())
    ref, hyp = norm(expected), norm(read)
    if not case:
        ref, hyp = ref.lower(), hyp.lower()
    lines = _changed_lines(ref, hyp)
    digits = lambda t: re.sub(r"\D", "", t)
    letters = lambda t: re.sub(r"[\W\d_]", "", t)
    char_errors = sum(_bounded(x.replace("\n", ""), y.replace("\n", "")) for x, y in lines)
    letter_errors = sum(_bounded(letters(x), letters(y)) for x, y in lines)
    word_errors = sum(_bounded(x.split(), y.split()) for x, y in lines)
    digit_errors = sum(_bounded(digits(x), digits(y)) for x, y in lines)
    order, free_errors = _reading_order(ref.split("\n"), hyp.split("\n")) if lines else (1.0 if ref else None, 0)
    conf, word_conf = _confusions(lines) if free_errors else ({}, {})  # only moved: nothing misread
    score = OcrScore(len(ref.replace("\n", "")), char_errors, len(ref.split()), word_errors, len(digits(ref)),
                     digit_errors, lines, order, free_errors, len(letters(ref)), letter_errors, conf, word_conf)
    if run is not None:
        out_of_order = min_order is not None and order is not None and order < min_order
        bad = score.cer > max_cer or (max_digit_errors is not None and digit_errors > max_digit_errors) or out_of_order
        # Out of order but read right: the lines "changed" are only moved, and no example of them helps.
        worst = [] if free_errors == 0 else [f"{y!r} for {x!r}" for x, y in lines[:3]]
        why = (f"character error rate {score.cer:.1%} (over {max_cer:.0%})" if score.cer > max_cer else
               f"{digit_errors} wrong digit{'s' * (digit_errors != 1)}" if max_digit_errors is not None
               and digit_errors > max_digit_errors else f"{order:.0%} of lines in reading order")
        if score.cer > max_cer and order is not None and order < 0.9:
            why += f"; {order:.0%} of lines in reading order, {score.order_free_cer:.1%} wrong with them put back"
        run.check("ocr" if page is None else f"ocr page {page}", "fail" if bad else "pass", evaluator=EVALUATOR,
                  reason=(why + (": " + "; ".join(worst) if worst and not why.endswith("reading order") else ""))
                  if bad else None,
                  category="ocr" if bad else None,
                  raw_output=json.dumps({"kind": "ocr", "chars": score.chars, "char_errors": char_errors,
                                         "words": score.words, "word_errors": word_errors, "digits": score.digits,
                                         "digit_errors": digit_errors, "worst": worst, "order": order,
                                         "order_free_errors": free_errors, "letters": score.letters,
                                         "letter_errors": letter_errors, "confusions": _top(conf, 30),
                                         "word_confusions": _top(word_conf, 15)}))
    return score


# ---------- OCR without labels: engines ranked against corrected text (DocOCR-Eval) ----------

def anls(reference: str, read: str, tau: float = 0.5) -> float:
    """Normalized Levenshtein similarity of a page's text, 1 - edits over the longer: 1.0 identical,
    0.0 below `tau` (a page read as something else entirely scores nothing, as ANLS does). Spacing
    doesn't count; compared line by line, so a long page is quick."""
    norm = lambda t: "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in str(t or "").splitlines() if ln.strip())
    a, b = norm(reference), norm(read)
    longest = max(len(a.replace("\n", "")), len(b.replace("\n", "")))
    if not longest:
        return 1.0
    edits = sum(_bounded(x.replace("\n", ""), y.replace("\n", "")) for x, y in _changed_lines(a, b))
    sim = 1 - min(edits, longest) / longest
    return sim if sim >= tau else 0.0


CORRECT_PROMPT = """This is the text an OCR engine read from one document page.

Correct only OCR errors: characters misread (l and 1, 0 and O, rn and m, 5 and S), words broken \
apart or run together, garbled tokens. If the page image is attached, read the page to settle any \
doubtful text. Keep everything else as it is: the line breaks and their order, the page's own \
spelling, and any value you can't verify. Don't add, remove, summarize, translate or reformat.

Return only the corrected text, with nothing before or after it."""

_FALLBACK_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5")


class OcrCorrectionError(RuntimeError):
    """The corrector gave no text: an API error, a refusal, or an empty answer."""


def correct_ocr(read: str, corrector: Any = None, image: Any = None, media_type: str = "image/png") -> str:
    """The OCR text with its errors corrected by a model, as DocOCR-Eval's pseudo-reference: what
    the page most likely says, for ranking OCR engines without labels (rank_ocr). Never use it as
    ground truth: it's a model's reading.

    corrector: an assay_sdk.Judge (any provider), or None for Claude (claude-opus-5-5). image: the
    page (bytes, or a path), so the model can re-read doubtful text; sent to Claude as an image
    block, and text-only to other providers."""
    from assay_sdk.llm import Judge
    judge = corrector if corrector is not None else Judge("anthropic", "claude-opus-5-5")
    content: Any = f"{CORRECT_PROMPT}\n\n<ocr>\n{read}\n</ocr>"
    params: Dict[str, Any] = {}
    if judge.provider == "anthropic":
        if image is not None:
            import base64
            data = image if isinstance(image, (bytes, bytearray)) else open(image, "rb").read()
            content = [{"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                    "data": base64.standard_b64encode(data).decode("ascii")}},
                       {"type": "text", "text": content}]
        if judge.model in _FALLBACK_MODELS:  # a declined request is re-run on the model Anthropic picks
            params.update(extra_headers={"anthropic-beta": "server-side-fallback-2026-07-01"},
                          extra_body={"fallbacks": "default"})
    r = judge.ask([{"role": "user", "content": content}], **params)
    if r.error or r.finish_reason == "refusal" or not (r.text or "").strip():
        raise OcrCorrectionError(r.error or ("the corrector declined" if r.finish_reason == "refusal"
                                             else "the corrector returned no text"))
    if r.finish_reason == "length":
        raise OcrCorrectionError("the correction was cut off at max_tokens: split the page, or raise max_tokens")
    return re.sub(r"^\s*</?ocr>\s*|\s*</?ocr>\s*$", "", r.text.strip())


@dataclass
class OcrRanking:
    scores: Dict[str, float]  # engine -> mean ANLS against the corrected text, over pages and correctors
    by_corrector: Dict[str, Dict[str, float]]  # corrector -> engine -> mean ANLS
    order: List[str]  # engines, best first
    pages: int
    truth: Optional[Dict[str, float]] = None  # engine -> mean ANLS against labels, on the labelled pages
    labelled: int = 0
    kendall: Optional[float] = None  # agreement of the two orders, -1 to 1
    ndcg: Optional[float] = None  # the order's gain against labelled ANLS, 1 at best

    @property
    def same_best(self) -> Optional[bool]:
        return None if self.truth is None else self.order[0] == max(self.truth, key=self.truth.get)


def _kendall(a: Dict[str, float], b: Dict[str, float]) -> Optional[float]:
    keys = sorted(set(a) & set(b))
    pairs = [(x, y) for i, x in enumerate(keys) for y in keys[i + 1:]]
    if not pairs:
        return None
    sign = lambda v: (v > 0) - (v < 0)
    return sum(sign(a[x] - a[y]) * sign(b[x] - b[y]) for x, y in pairs) / len(pairs)


def _ndcg(order: List[str], gain: Dict[str, float]) -> Optional[float]:
    import math
    dcg = lambda seq: sum(gain.get(e, 0.0) / math.log2(i + 2) for i, e in enumerate(seq))
    best = dcg(sorted(gain, key=gain.get, reverse=True))
    return dcg([e for e in order if e in gain]) / best if best else None


def rank_ocr(run, engines: Dict[str, Dict[Any, str]], corrected: Dict[str, Dict[Any, str]],
             truth: Optional[Dict[Any, str]] = None, same_model: Optional[Dict[str, str]] = None,
             tau: float = 0.5, name: str = "ocr ranking") -> OcrRanking:
    """Rank OCR engines without labels, as DocOCR-Eval does: each page's text as each engine read
    it, against the same page corrected by one or more models (correct_ocr), scored by ANLS and
    averaged over the correctors so no one model's taste decides. An engine missing a page scores
    0 on it.

    engines: {engine: {page: text}}. corrected: {corrector: {page: corrected text}}. truth: the
    labelled pages you do have, {page: text}: the ranking is then checked against theirs (Kendall
    tau, NDCG, the same best engine), which says how far to trust it on the rest.

    An engine scored against its own corrections is flattered, so it's refused: an engine named as
    a corrector, or same_model={engine: corrector} for one built on the corrector's model.
    Recorded as the check `ocr ranking`, apart from score_ocr: its reference is a model's reading,
    never counted as ground truth."""
    same_model = dict(same_model or {})
    clash = sorted({e for e in engines if e in corrected} | {e for e, c in same_model.items() if c in corrected})
    if clash:
        raise ValueError(f"rank_ocr: {', '.join(clash)} would be scored against its own corrections, which "
                         "flatters it. Leave that corrector out, or that engine.")
    if not corrected:
        raise ValueError("rank_ocr: no corrected text; correct each page first (correct_ocr)")
    by_corrector = {}
    for c, pages in corrected.items():
        by_corrector[c] = {e: sum(anls(ref, read.get(p, ""), tau) for p, ref in pages.items()) / len(pages)
                           for e, read in engines.items()} if pages else {}
    scores = {e: sum(bc[e] for bc in by_corrector.values() if e in bc) / len(by_corrector) for e in engines}
    order = sorted(scores, key=lambda e: (-scores[e], e))
    pages = len({p for pages in corrected.values() for p in pages})
    out = OcrRanking(scores, by_corrector, order, pages)
    if truth:
        out.truth = {e: sum(anls(ref, read.get(p, ""), tau) for p, ref in truth.items()) / len(truth)
                     for e, read in engines.items()}
        out.labelled, out.kendall, out.ndcg = len(truth), _kendall(scores, out.truth), _ndcg(order, out.truth)
    if run is not None:
        agree = out.kendall is None or out.same_best
        best = max(out.truth, key=out.truth.get) if out.truth else None
        run.check(name, "pass" if agree else "fail", evaluator=EVALUATOR,
                  reason=None if agree else f"on the {out.labelled} labelled pages, {best} is best, not "
                  f"{order[0]}: the ranking without labels can't be trusted here",
                  category=None if agree else "ocr_ranking",
                  raw_output=json.dumps({"kind": "ocr_rank", "scores": {e: round(v, 6) for e, v in scores.items()},
                                         "order": order, "pages": pages, "correctors": sorted(corrected),
                                         "truth": {e: round(v, 6) for e, v in (out.truth or {}).items()} or None,
                                         "labelled": out.labelled, "kendall": out.kendall, "ndcg": out.ndcg}))
    return out


# ---------- where on the page a value was found ----------

def _box(b: Any, fmt: str) -> Optional[Tuple[float, float, float, float]]:
    if b is None:
        return None
    x = [float(v) for v in (b.values() if isinstance(b, dict) else b)]
    if len(x) != 4:
        raise ValueError(f"a box is 4 numbers, not {b!r}")
    if fmt == "xywh":
        x = [x[0], x[1], x[0] + x[2], x[1] + x[3]]
    return min(x[0], x[2]), min(x[1], x[3]), max(x[0], x[2]), max(x[1], x[3])


def iou(a: Tuple[float, float, float, float], b: Tuple[float, float, float, float]) -> float:
    """Intersection over union of two boxes (x0, y0, x1, y1)."""
    w = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    h = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = w * h
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def score_locations(run, expected: Dict[str, Any], extracted: Dict[str, Any], min_iou: float = 0.5,
                    box: str = "xyxy") -> Dict[str, Tuple[Optional[bool], str]]:
    """Whether each field was read from the right place: the same page, and a box overlapping the
    correct one by at least `min_iou` (intersection over union). Locations are {"page": 2, "bbox":
    [x0, y0, x1, y1]} (box="xywh" for x, y, width, height), in the same units on both sides.
    Recorded as `location: <field>` checks."""
    out = {}
    for name, e in expected.items():
        a = (extracted or {}).get(name)
        if empty(e):
            continue
        ep, ab = e.get("page") if isinstance(e, dict) else None, a.get("page") if isinstance(a, dict) else None
        eb = _box(e.get("bbox") if isinstance(e, dict) else e, box)
        if empty(a):
            ok, why, overlap = False, "no location given", None
        elif ep is not None and ab is not None and int(ep) != int(ab):
            ok, why, overlap = False, f"on page {ab}, not {ep}", 0.0
        else:
            xb = _box(a.get("bbox") if isinstance(a, dict) else a, box)
            overlap = iou(eb, xb) if eb and xb else None
            ok = overlap is not None and overlap >= min_iou
            why = "" if ok else f"overlaps the right box by {overlap:.2f} (under {min_iou:g})" if overlap is not None \
                else "no box"
        out[name] = (ok, why)
        if run is not None:
            run.check(f"location: {name}", "pass" if ok else "fail", evaluator=EVALUATOR, reason=why or None,
                      category=None if ok else "location",
                      raw_output=json.dumps({"kind": "location", "iou": overlap, "page_right": not why.startswith("on page")}))
    return out


# ---------- a value that's on the page: a rule, with no correct values ----------

_NUMBERS = re.compile(r"\(?-?[$€£¥₹]?\s?\d[\d.,' ]*\d(?:\s?[$€£¥₹])?\)?|\d")
_DATEISH = re.compile(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b|\b\d{1,2}(?:st|nd|rd|th)? [A-Za-z]{3,9}\.? \d{4}\b|"
                      r"\b[A-Za-z]{3,9}\.? \d{1,2}(?:st|nd|rd|th)?,? \d{4}\b")


def _found(spec: _Field, value: Any, text: str) -> bool:
    if isinstance(spec, Date):
        want = spec.read(value)
        for m in _DATEISH.finditer(text):
            try:
                if spec.read(m.group(0)) == want:
                    return True
            except Unreadable:
                pass
        return False
    if isinstance(spec, Number):
        want = spec.read(value)
        want = want[0] if isinstance(want, tuple) else want
        for m in _NUMBERS.finditer(text):
            try:
                got = spec.read(m.group(0).strip())
            except (Unreadable, ValueError):
                continue
            if abs((got[0] if isinstance(got, tuple) else got) - want) <= max(getattr(spec, "tolerance", 0), 1e-9):
                return True
        return False
    return Text().read(value) in Text().read(text)


def appears_in(text: str, schema: Dict[str, _Field], fields: Optional[Sequence[str]] = None) -> Rule:
    """Each extracted value appears in the document's text (its OCR), read by its type: 1234.56 is
    found as "1,234.56", a date as "4 March 2026". A value that's nowhere on the page was made up
    or read from somewhere else. Needs no correct values, so it runs on production documents."""
    names = list(fields or [k for k, v in schema.items() if not isinstance(v, LineItems)])

    def check(doc):
        gone = []
        for n in names:
            v = _get(doc, n)
            if empty(v):
                continue
            try:
                if not _found(schema.get(n) or Text(), v, text or ""):
                    gone.append(f"{n} {v!r}")
            except Unreadable:
                gone.append(f"{n} {v!r} (unreadable)")
        return (not gone, "not in the document's text: " + ", ".join(gone) if gone else "")
    return Rule("values appear in the text", check)


# ---------- tables: structure as well as cells ----------

@dataclass
class TableScore:
    name: str
    shape_right: bool  # as many rows and columns, the same header
    cells: int  # the correct table's body cells
    cells_read: int  # the body cells of the columns and rows that were matched, as read
    cells_right: int
    rows: Dict[str, int]  # tp, fp, fn over rows (right when every cell is)
    notes: List[str]
    teds: float = 1.0  # tree edit distance similarity: structure and text
    teds_structure: float = 1.0  # the same, structure only (TEDS-S)

    @property
    def precision(self) -> float:
        return self.cells_right / self.cells_read if self.cells_read else (1.0 if not self.cells else 0.0)

    @property
    def recall(self) -> float:
        return self.cells_right / self.cells if self.cells else 1.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    @property
    def correct(self) -> bool:
        return self.shape_right and self.cells_right == self.cells and not self.notes


def _ted(a: tuple, b: tuple, rename: Callable[[Any, Any], float]) -> float:
    """Tree edit distance (Zhang and Shasha): trees are (label, [children]); inserting or deleting
    a node costs 1, renaming one `rename(label, label)`."""
    def post(t):
        labels, lmd = [], []

        def walk(node):
            first = None
            for ch in node[1]:
                f = walk(ch)
                first = f if first is None else first
            labels.append(node[0])
            lmd.append(len(labels) - 1 if first is None else first)
            return lmd[-1]
        walk(t)
        keys = sorted({max(i for i in range(len(lmd)) if lmd[i] == l) for l in set(lmd)})
        return labels, lmd, keys
    la, ma, ka = post(a)
    lb, mb, kb = post(b)
    td = [[0.0] * len(lb) for _ in la]
    for i in ka:
        for j in kb:
            li, lj = ma[i], mb[j]
            fd = [[0.0] * (j - lj + 2) for _ in range(i - li + 2)]
            for x in range(1, i - li + 2):
                fd[x][0] = fd[x - 1][0] + 1
            for y in range(1, j - lj + 2):
                fd[0][y] = fd[0][y - 1] + 1
            for x in range(1, i - li + 2):
                for y in range(1, j - lj + 2):
                    ix, jy = li + x - 1, lj + y - 1
                    if ma[ix] == li and mb[jy] == lj:
                        fd[x][y] = min(fd[x - 1][y] + 1, fd[x][y - 1] + 1,
                                       fd[x - 1][y - 1] + rename(la[ix], lb[jy]))
                        td[ix][jy] = fd[x][y]
                    else:
                        fd[x][y] = min(fd[x - 1][y] + 1, fd[x][y - 1] + 1,
                                       fd[ma[ix] - li][mb[jy] - lj] + td[ix][jy])
    return td[-1][-1]


def teds(expected: Sequence[Sequence[Any]], extracted: Sequence[Sequence[Any]], structure_only: bool = False) -> float:
    """Tree edit distance similarity, the standard table score: both tables as trees (table, rows,
    cells), 1 - their edit distance over the larger's size. A cell renamed costs its text's
    normalized edit distance (case and spacing ignored); structure_only: nothing (TEDS-S)."""
    def tree(t):
        return ("table", [("tr", [(("td", Text().read(c) if not empty(c) else ""), []) for c in r]) for r in t or []])

    def rename(x, y):
        if isinstance(x, tuple) and isinstance(y, tuple):
            if structure_only or x[1] == y[1]:
                return 0.0
            return _edits(x[1], y[1]) / max(len(x[1]), len(y[1]))
        return 0.0 if x == y else 1.0
    a, b = tree(expected), tree(extracted)
    size = lambda t: 1 + sum(size(c) for c in t[1])
    n = max(size(a), size(b))
    return 1.0 - _ted(a, b, rename) / n if n else 1.0


def score_table(run, expected: Sequence[Sequence[Any]], extracted: Sequence[Sequence[Any]], name: str = "table",
                header: bool = True, cells: Union[_Field, Dict[str, _Field]] = None) -> TableScore:
    """Score a table's structure and its cells, recorded as the check `table: <name>`. Tables are
    lists of rows of cells; with `header`, the first row names the columns and columns are matched
    by name (so a column moved is still the same column), else by position. Rows are matched by
    cells in common, whatever their order. Says what happened to the structure: a column missing,
    added, or two merged into one; rows missing or added. The cells score is the F1 of the cells
    right over those of the correct table and those read: one number that a lost column and a
    garbled cell both lower. Beside it, TEDS (tree edit distance similarity), the standard score,
    and TEDS-S, structure only. `cells`: the type of every cell, or per column name."""
    exp = [list(r) for r in (expected or [])]
    got = [list(r) for r in (extracted or [])]
    spec_of = (lambda c: (cells.get(c) if isinstance(cells, dict) else cells) or Text())
    notes: List[str] = []
    if header and exp:
        eh, gh = [Text().read(h) for h in exp[0]], [Text().read(h) for h in got[0]] if got else []
        body_e, body_g = exp[1:], got[1:]
        cols = {i: gh.index(h) for i, h in enumerate(eh) if h in gh}  # correct column -> read column
        for j, h in enumerate(gh):
            if j in cols.values():
                continue
            parts = [i for i in range(len(eh) - 1) if f"{eh[i]} {eh[i + 1]}" == h and i not in cols and i + 1 not in cols]
            if parts:
                notes.append(f"columns {exp[0][parts[0]]!r} and {exp[0][parts[0] + 1]!r} merged into one")
            else:
                notes.append(f"a column that isn't there: {got[0][j]!r}")
        merged = {i for n in notes if n.startswith("columns ") for i in range(len(eh))
                  if f"{exp[0][i]!r}" in n}
        notes += [f"column {exp[0][i]!r} missing" for i in range(len(eh)) if i not in cols and i not in merged]
        names = [str(h) for h in exp[0]]
        header_right = sorted(eh) == sorted(gh)  # the same columns; their order isn't structure
    else:
        body_e, body_g = exp, got
        width = max((len(r) for r in exp), default=0)
        cols = {i: i for i in range(width) if any(len(r) > i for r in got)}
        if max((len(r) for r in got), default=0) != width:
            notes.append(f"{max((len(r) for r in got), default=0)} columns, not {width}")
        names = [str(i) for i in range(width)]
        header_right = True
    cell = lambda row, i: row[i] if i is not None and i < len(row) else None
    ok = lambda i, e_row, g_row: _cell_ok(spec_of(names[i]), cell(e_row, i), cell(g_row, cols.get(i)))
    same = {(a, b): sum(ok(i, e, g) for i in cols) for a, e in enumerate(body_e) for b, g in enumerate(body_g)}
    matched, ue, ug = [], set(), set()
    if not cols:  # no column in common (merged, renamed): nothing to match rows by but their place
        matched = list(zip(range(len(body_e)), range(len(body_g))))
        ue, ug = {a for a, _ in matched}, {b for _, b in matched}
    else:
        matched = _assign([[same[(a, b)] for b in range(len(body_g))] for a in range(len(body_e))])
        ue, ug = {a for a, _ in matched}, {b for _, b in matched}
    dup = _duplicates([body_g[b] for b in range(len(body_g)) if b not in ug], [body_g[b] for b in ug],
                      lambda r, m: [Text().read(x) for x in r] == [Text().read(x) for x in m])
    if len(body_e) - len(ue):
        notes.append(f"{len(body_e) - len(ue)} row(s) missing")
    if len(body_g) - len(ug) - dup:
        notes.append(f"{len(body_g) - len(ug) - dup} row(s) that aren't there")
    if dup:
        notes.append(f"{dup} row(s) duplicated")
    width = len(names)
    right = sum(ok(i, body_e[a], body_g[b]) for a, b in matched for i in cols)
    read_cells = sum(len(r) for r in body_g)
    rows_right = sum(1 for a, b in matched if same[(a, b)] == width)
    score = TableScore(name, header_right and len(body_e) == len(body_g) and len(cols) == width and
                       all(len(r) == width for r in body_g), len(body_e) * width, read_cells, right,
                       {"tp": rows_right, "fp": len(body_g) - rows_right, "fn": len(body_e) - rows_right}, notes,
                       teds(exp, got), teds(exp, got, structure_only=True))
    wrong = [(a, b, i) for a, b in matched for i in cols if not ok(i, body_e[a], body_g[b])]
    if wrong and not score.correct:
        a, b, i = wrong[0]
        notes.append(f"{len(wrong)} cell(s) wrong, e.g. {names[i]} in row {a + 1}: "
                     f"{cell(body_g[b], cols.get(i))!r}, not {cell(body_e[a], i)!r}")
    if run is not None:
        run.check(f"table: {name}", "pass" if score.correct else "fail", evaluator=EVALUATOR,
                  reason=None if score.correct else "; ".join(notes[:4]),
                  category=None if score.correct else "table",
                  raw_output=json.dumps({"kind": "table", "shape_right": score.shape_right, "cells": score.cells,
                                         "cells_read": read_cells, "cells_right": right, **score.rows,
                                         "teds": round(score.teds, 6),
                                         "teds_structure": round(score.teds_structure, 6)}))
    return score


# ---------- spot checks: published values, verified afterwards ----------

SPOT_CHECKS = "spot-checks"


def spot_check(document_id: str, field: str, published: Any, correct: Any, spec: Optional[_Field] = None,
               reviewed: Optional[bool] = None, auto_approved: Optional[bool] = None,
               checked_by: Optional[str] = None) -> bool:
    """A value that reached published output, verified afterwards by a person: right or not. Sent as
    a check of the run `spot-checks` against the production document, so the dashboard's **Escape
    rate** says how often a wrong value got through both automation and review, and by which way
    it went out (reviewed, or auto-approved). `spec`: how to compare (Text by default)."""
    from assay_sdk import check
    f = _score_value(field, spec or Text(), correct, published)
    if f.kind == "unreadable":
        return False
    path = "reviewed" if reviewed else "auto-approved" if auto_approved else None
    check(SPOT_CHECKS, f"{document_id}:{field}", "pass" if f.passed else "fail", run_id=document_id, field=field,
          expected=correct, actual=published, evaluator="assay.spotcheck@1",
          reason=None if f.passed else f"published {f.kind}: {f.note}" if f.note else f"published {f.kind}",
          category=None if f.passed else f.kind,
          raw_output=json.dumps({"kind": "spot_check", "path": path, "by": checked_by, "wrong": not f.passed}))
    return bool(f.passed)



# ---------- superseded values: a later document replaced them; did output follow? ----------

SUPERSEDED = "superseded"


def superseded_values(old_document_id: str, new_document_id: str, old: Dict[str, Any], new: Dict[str, Any],
                      output: Dict[str, Any], flagged: Union[bool, Sequence[str]] = (),
                      schema: Optional[Dict[str, _Field]] = None, link: str = "replaces",
                      checked_by: Optional[str] = None) -> Dict[str, str]:
    """A later document amends or replaces an earlier one (a corrected invoice, an amended
    contract). For each field whose value it changed: what does output hold for the earlier
    document now?

      updated    the new value: output followed
      flagged    still the old value, but marked as superseded or held for review
      escaped    the old value (or another wrong one), unmarked: the costly case, since whatever
                 reads output takes it as current

    `flagged`: True for the whole document, or the fields marked. `link`: "replaces" or
    "amends". Sent as checks of the run `superseded` against the earlier document, so the
    dashboard's **Superseded values reaching output** is the share escaped. Fields the new
    document left unchanged aren't counted. Returns {field: outcome}."""
    from assay_sdk import check
    schema = schema or {}
    names = [k for k in dict.fromkeys(list(new) + list(schema))]
    marked = set(names) if flagged is True else set(flagged or ())
    out = {}
    for name in names:
        spec = schema.get(name) or Text()
        before, after, now = _get(old, name), _get(new, name), _get(output, name)
        if isinstance(spec, LineItems) or empty(after) and empty(before):
            continue
        if _cell_ok(spec, before, after):
            continue  # not superseded: the new document says the same
        if _cell_ok(spec, after, now):  # the new value, or nothing when the new document dropped it
            outcome = "updated"
        elif name in marked:
            outcome = "flagged"
        else:
            outcome = "escaped"
        out[name] = outcome
        stale = "the old value" if _cell_ok(spec, before, now) else "neither value" if not empty(now) else "nothing"
        check(SUPERSEDED, f"{old_document_id}->{new_document_id}:{name}", "fail" if outcome == "escaped" else "pass",
              run_id=old_document_id, field=name, expected=after, actual=now, evaluator="assay.superseded@1",
              reason=f"output holds {stale} ({now!r}); {new_document_id} {link} it with {after!r}"
              if outcome == "escaped" else None, category="superseded" if outcome == "escaped" else None,
              raw_output=json.dumps({"kind": "superseded", "outcome": outcome, "link": link,
                                     "new_document": new_document_id, "by": checked_by}))
    return out
