"""`assay connect`: attach Assay to a pipeline with the least work, and see that it worked.

    assay connect                 what's here, and the ways in, the least work first
    assay connect db [URL]        read the database's schema, write the mapping, test it
    assay connect code [PATH]     the smallest code change, as a diff to review (--apply writes it)
    assay connect verify          the pipeline Assay found, and the steps it hasn't seen yet

The database comes first: it needs no change to the pipeline at all. Then tracing that's
already there, then a few lines of code. Code is never changed without --apply, and --apply
shows the same diff it writes.
"""
from __future__ import annotations

import ast
import difflib
import shlex
import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SKIP_DIRS = {".git", ".venv", "venv", "env", "node_modules", "site-packages", "build", "dist", "__pycache__",
             ".assay", ".mypy_cache", ".pytest_cache", ".tox", "migrations"}
FRAMEWORKS = {"anthropic": "Anthropic SDK", "openai": "OpenAI SDK", "langgraph": "LangGraph", "langchain": "LangChain",
              "langchain_core": "LangChain", "langchain_openai": "LangChain", "langchain_anthropic": "LangChain",
              "llama_index": "LlamaIndex", "litellm": "LiteLLM", "crewai": "CrewAI", "dspy": "DSPy",
              "haystack": "Haystack", "autogen": "AutoGen", "pydantic_ai": "Pydantic AI"}
LLM_CALLS = (".messages.create", ".messages.stream", ".chat.completions.create", ".responses.create",
             ".completions.create")
INIT_BLOCK = ["import assay_sdk as assay", "",
              "assay.init()        # sends to ASSAY_URL (with ASSAY_KEY), or records to .assay/events.jsonl",
              "assay.instrument()  # records Anthropic and OpenAI calls, in the step they're made in"]


# ---------- the code ----------

@dataclass
class Found:
    root: Path
    files: int = 0
    frameworks: Dict[str, set] = field(default_factory=lambda: defaultdict(set))  # label -> files
    llm_calls: List[tuple] = field(default_factory=list)  # (file, line, enclosing function)
    graph_nodes: List[tuple] = field(default_factory=list)  # (file, line, node name, function name)
    tools: List[tuple] = field(default_factory=list)  # (file, line, function name) with a @tool decorator
    otel: set = field(default_factory=set)
    attached: set = field(default_factory=set)  # files that already use assay_sdk
    steps_in_code: set = field(default_factory=set)  # names already decorated with @assay.step
    entries: List[str] = field(default_factory=list)
    invokes: List[tuple] = field(default_factory=list)  # (file, line, enclosing function): graph.invoke(...) and the like
    calls_by_fn: Dict[str, set] = field(default_factory=lambda: defaultdict(set))  # function -> names it calls
    run_starts: set = field(default_factory=set)  # functions already starting runs (@assay.pipeline/agent, assay.run)
    defs: Dict[str, List[tuple]] = field(default_factory=lambda: defaultdict(list))  # name -> [(file, line, col, decorated)]
    sources: Dict[str, str] = field(default_factory=dict)


def _py_files(root: Path):
    if root.is_file():
        yield root
        return
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                yield Path(dirpath) / f


def _dotted(node) -> str:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_assay(dec) -> bool:
    target = dec.func if isinstance(dec, ast.Call) else dec
    return _dotted(target).startswith("assay.")


def scan_code(root: Path) -> Found:
    found = Found(root=root)
    for path in _py_files(root):
        rel = os.path.relpath(path, Path.cwd()) if path.is_absolute() else str(path)
        try:
            text = path.read_text()
            tree = ast.parse(text)
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        found.files += 1
        found.sources[rel] = text
        stack: List[str] = []

        class V(ast.NodeVisitor):
            def visit_Import(self, node):
                for a in node.names:
                    self._module(a.name)

            def visit_ImportFrom(self, node):
                self._module(node.module or "")

            def _module(self, name):
                top = name.split(".")[0]
                if top in FRAMEWORKS:
                    found.frameworks[FRAMEWORKS[top]].add(rel)
                if top == "opentelemetry":
                    found.otel.add(rel)
                if top == "assay_sdk":
                    found.attached.add(rel)

            def _def(self, node):
                decorated = any(_is_assay(d) for d in node.decorator_list)
                first = min([d.lineno for d in node.decorator_list] + [node.lineno])
                found.defs[node.name].append((rel, node.lineno, node.col_offset, decorated, first))
                for d in node.decorator_list:
                    t = d.func if isinstance(d, ast.Call) else d
                    name = _dotted(t)
                    if name in ("tool", "langchain.tools.tool", "langchain_core.tools.tool") or name.endswith(".tool") \
                            and not name.startswith("assay."):
                        found.tools.append((rel, node.lineno, node.name))
                    if name in ("assay.pipeline", "assay.agent"):
                        found.run_starts.add(node.name)
                    if name == "assay.step":
                        arg = d.args[0].value if isinstance(d, ast.Call) and d.args and isinstance(d.args[0], ast.Constant) else node.name
                        found.steps_in_code.add(str(arg))
                stack.append(node.name)
                self.generic_visit(node)
                stack.pop()
            visit_FunctionDef = visit_AsyncFunctionDef = _def

            def visit_Call(self, node):
                name = _dotted(node.func)
                if any(("." + name).endswith(c) for c in LLM_CALLS) or name in ("litellm.completion", "litellm.acompletion"):
                    found.llm_calls.append((rel, node.lineno, stack[-1] if stack else None))
                if stack:
                    found.calls_by_fn[stack[-1]].add(name.split(".")[-1])
                    if name == "assay.run":
                        found.run_starts.add(stack[-1])
                recv = name.rsplit(".", 1)[0].split(".")[-1].lower() if "." in name else ""
                if name.split(".")[-1] in ("invoke", "ainvoke", "stream", "astream", "batch", "kickoff") and \
                        any(w in recv for w in ("graph", "agent", "chain", "executor", "app", "crew", "workflow")):
                    found.invokes.append((rel, node.lineno, stack[-1] if stack else None))
                if name.endswith(".add_node") and len(node.args) >= 2 and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[1], ast.Name):
                    found.graph_nodes.append((rel, node.lineno, str(node.args[0].value), node.args[1].id))
                if name in ("FastAPI", "Flask", "typer.Typer", "fastapi.FastAPI", "flask.Flask"):
                    found.entries.append(rel)
                self.generic_visit(node)

            def visit_If(self, node):
                t = node.test
                if isinstance(t, ast.Compare) and isinstance(t.left, ast.Name) and t.left.id == "__name__":
                    found.entries.append(rel)
                self.generic_visit(node)
        V().visit(tree)
    return found


def _import_line(text: str) -> int:
    """The line after the module's last top-level import (1-based), past a docstring."""
    tree = ast.parse(text)
    last = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            last = node.end_lineno
        elif last and not isinstance(node, (ast.Import, ast.ImportFrom)):
            break
    if not last and tree.body and isinstance(tree.body[0], ast.Expr) and isinstance(getattr(tree.body[0], "value", None), ast.Constant):
        last = tree.body[0].end_lineno  # after the docstring
    return last


def propose(found: Found) -> Tuple[Dict[str, str], List[str]]:
    """({file: new text}, what each change is). Nothing is written here."""
    inserts: Dict[str, List[Tuple[int, List[str]]]] = defaultdict(list)  # file -> [(insert before line, lines)]
    notes: List[str] = []
    done = set()

    def decorate(fn: str, line: str, why: str, tool: bool = False):
        for rel, lineno, col, decorated, first in found.defs.get(fn, []):
            if decorated or (rel, fn) in done:
                continue
            done.add((rel, fn))
            at = lineno if tool else first  # a tool's goes right above `def`, inside other decorators
            inserts[rel].append((at, [" " * col + line]))
            notes.append(f"{rel}: {why} {fn}")
    for rel, _, node, fn in found.graph_nodes:
        decorate(fn, f'@assay.step("{node}")', "@assay.step on graph node")
    for rel, _, fn in sorted({(r, 0, f) for r, _, f in found.llm_calls if f}):
        decorate(fn, f'@assay.step("{fn}")', "@assay.step on", )
    for rel, _, fn in found.tools:
        decorate(fn, "@assay.tool", "@assay.tool on", tool=True)
    # Where one run begins: what invokes the graph or agent, else what calls the steps. Without it,
    # each step would be a run of its own.
    steps = {fn for _, _, _, fn in found.graph_nodes} | {fn for _, _, fn in found.llm_calls if fn}
    kind = "agent" if found.tools and not found.graph_nodes else "pipeline"
    entries = [fn for _, _, fn in found.invokes if fn] or \
        [f for f, called in found.calls_by_fn.items() if f not in steps and called & steps]
    for fn in dict.fromkeys(entries):
        if fn not in found.run_starts:
            decorate(fn, f'@assay.{kind}("{fn}")', f"@assay.{kind} (one run per call) on")
    touched = set(inserts)
    entry = next((e for e in found.entries if e in found.sources), None) or \
        (Counter(r for r, _, _ in found.llm_calls).most_common(1) or [(None, 0)])[0][0] or next(iter(touched), None)
    if entry and not (found.attached & {entry}):
        at = _import_line(found.sources[entry]) + 1
        inserts[entry].append((at, ["", *INIT_BLOCK]))
        notes.insert(0, f"{entry}: assay.init() and assay.instrument()")
    for rel in touched - {entry} - found.attached:
        inserts[rel].append((_import_line(found.sources[rel]) + 1, ["import assay_sdk as assay"]))
    out = {}
    for rel, ins in inserts.items():
        lines = found.sources[rel].split("\n")
        for at, new in sorted(ins, key=lambda x: -x[0]):
            lines[at - 1:at - 1] = new
        out[rel] = "\n".join(lines)
    return out, notes


def diff(found: Found, changes: Dict[str, str]) -> str:
    parts = []
    for rel in sorted(changes):
        a, b = found.sources[rel].splitlines(keepends=True), changes[rel].splitlines(keepends=True)
        parts.append("".join(difflib.unified_diff(a, b, f"a/{rel}", f"b/{rel}")))
    return "".join(parts)


# ---------- the database ----------

RECORDS = {  # record: (table names, {field: column names, best first}, fields it can't do without)
    "documents": (["documents", "document", "docs", "files", "uploads", "submissions", "records", "items"], {
        "document_id": ["document_id", "doc_id", "id", "file_id", "uuid"],
        "received_at": ["received_at", "created_at", "uploaded_at", "submitted_at", "inserted_at", "ingested_at"],
        "completed_at": ["completed_at", "finished_at", "processed_at", "done_at", "published_at"],
        "status": ["status", "state"], "processing_mode": ["processing_mode", "mode", "route", "routing"],
        "file_hash": ["file_hash", "sha256", "checksum", "hash", "md5"],
        "segment": ["segment", "customer", "customer_id", "tenant", "client", "account", "jurisdiction", "region", "county"],
        "document_type": ["document_type", "doc_type", "type", "category", "kind", "instrument_type"],
        "page_count": ["page_count", "pages", "num_pages", "n_pages"]}, ["document_id", "received_at"]),
    "stage_runs": (["stage_runs", "stage_executions", "stages", "steps", "step_runs", "pipeline_steps", "task_runs",
                    "executions", "job_steps", "runs"], {
        "document_id": ["document_id", "doc_id", "file_id"],
        "stage": ["stage", "step", "stage_name", "step_name", "task", "task_name", "node", "name"],
        "status": ["status", "state", "outcome", "result"],
        "started_at": ["started_at", "start_time", "started", "created_at", "begin_at"],
        "finished_at": ["finished_at", "ended_at", "end_time", "completed_at", "finished"],
        "did_work": ["did_work"], "outputs": ["outputs", "output", "result_json", "payload"],
        "sequence": ["sequence", "seq", "position", "step_index", "step_order"],
        "prompt_id": ["prompt_id", "prompt_name"], "prompt_version": ["prompt_version"]},
        ["document_id", "stage", "started_at"]),
    "calls": (["model_calls", "llm_calls", "calls", "llm_requests", "completions", "inferences", "ai_calls",
               "model_requests", "llm_logs"], {
        "call_id": ["call_id", "id", "request_id"], "stage": ["stage", "step", "stage_name"],
        "ts": ["ts", "created_at", "started_at", "timestamp", "requested_at"], "document_id": ["document_id", "doc_id"],
        "model_declared": ["model_declared", "model_requested", "requested_model", "model"],
        "model_served": ["model_served", "response_model", "served_model", "model"],
        "resolving_layer": ["resolving_layer", "layer", "tier"], "gate_reason": ["gate_reason", "fallback_reason"],
        "cost_usd": ["cost_usd", "cost", "price", "usd"],
        "code_revision": ["code_revision", "revision", "git_sha", "commit", "build"],
        "latency_ms": ["latency_ms", "duration_ms", "latency", "elapsed_ms"], "status": ["status", "state", "outcome"],
        "prompt_id": ["prompt_id", "prompt_name"], "prompt_version": ["prompt_version"]}, ["ts"]),
    "indexed": (["extractions", "extracted_fields", "fields", "extracted_values", "index_values", "entities"], {
        "document_id": ["document_id", "doc_id"],
        "has_positions": ["source_positions", "bbox", "bounding_box", "positions", "coordinates", "position"]},
        ["document_id"]),
    "reviews": (["reviews", "review_tasks", "hitl_tasks", "human_reviews", "annotations", "tasks"], {
        "review_id": ["review_id", "id", "task_id"], "document_id": ["document_id", "doc_id"],
        "ts": ["ts", "reviewed_at", "completed_at", "created_at"], "kind": ["kind", "review_type", "type"],
        "minutes": ["minutes", "duration_minutes", "time_spent_minutes"], "cost_usd": ["cost_usd", "cost"],
        "reviewer": ["reviewer", "reviewer_id", "assignee", "user_id"], "stage": ["stage"]}, ["document_id", "ts"]),
    "errors": (["corrections", "reported_errors", "field_corrections", "errors", "feedback", "issues"], {
        "error_id": ["error_id", "id"], "document_id": ["document_id", "doc_id"], "field": ["field", "field_name", "path"],
        "reported_at": ["reported_at", "created_at", "ts"],
        "expected": ["expected", "expected_value", "correct_value", "corrected_value"],
        "observed": ["observed", "observed_value", "actual", "original_value", "predicted"],
        "kind": ["kind", "error_type", "type"], "reporter": ["reporter", "reported_by", "user_id"], "source": ["source"]},
        ["document_id", "field"]),
}
ALIAS = {"documents": "d", "stage_runs": "s", "calls": "c", "indexed": "x", "reviews": "r", "errors": "e"}
EMPTY = "(SELECT 1 AS none WHERE 1=0)"  # a record type this database doesn't have: no rows


def _tables(engine) -> Dict[str, List[str]]:
    from sqlalchemy import inspect
    insp = inspect(engine)
    out = {}
    try:
        schemas = [s for s in insp.get_schema_names() if s not in ("information_schema", "pg_catalog", "pg_toast")]
    except Exception:
        schemas = [None]
    default = getattr(insp, "default_schema_name", None)
    for schema in schemas or [None]:
        for t in insp.get_table_names(schema=schema):
            name = t if schema in (None, default, "main") else f"{schema}.{t}"
            out[name] = [c["name"] for c in insp.get_columns(t, schema=schema)]
    return out


def _pick(cols: List[str], names: List[str]) -> Optional[Tuple[str, str]]:
    """(column, how sure) for a field: its own name, else a usual name, else one that contains it."""
    low = {c.lower(): c for c in cols}
    for i, n in enumerate(names):
        if n in low:
            return low[n], "exact" if i == 0 else "guessed"
    return None


def _score(table: str, cols: List[str], spec) -> Tuple[int, dict]:
    names, fields, required = spec
    base = table.split(".")[-1].lower()
    s = 6 if base in names else 3 if any(n.rstrip("s") in base for n in names) else 0
    picked = {f: _pick(cols, alts) for f, alts in fields.items()}
    if any(picked[f] is None for f in required):
        return -1, {}
    return s + sum(2 if p[1] == "exact" else 1 for p in picked.values() if p), picked


def map_database(engine) -> Tuple[dict, List[dict]]:
    """(mapping, what was decided for each record type)."""
    tables = _tables(engine)
    mapping, report, used = {}, [], set()
    for rec, spec in RECORDS.items():
        best = max(((t, *_score(t, c, spec)) for t, c in tables.items() if t not in used),
                   key=lambda x: x[1], default=(None, -1, {}))
        table, score, picked = best
        if table is None or score < 4:
            report.append({"record": rec, "table": None})
            continue
        used.add(table)
        a = ALIAS[rec]
        cols = {}
        for f, p in picked.items():
            if p is None:
                cols[f] = "NULL"
            elif f == "has_positions":
                cols[f] = f"({a}.{p[0]} IS NOT NULL)"
            else:
                cols[f] = f"{a}.{p[0]}"
        report.append({"record": rec, "table": table, "fields": {f: p for f, p in picked.items()}})
        mapping[rec] = {"from": f"{table} {a}", "columns": {**_nulls(rec), **cols}}
    docs = mapping.get("documents")
    for rec in ("calls", "indexed"):  # break these out by the document's segment and type
        m, r = mapping.get(rec), next(x for x in report if x["record"] == rec)
        if m and docs and m["columns"].get("document_id", "NULL") != "NULL":
            m["from"] += f" LEFT JOIN {docs['from']} ON {docs['columns']['document_id']} = {m['columns']['document_id']}"
            for f in ("segment", "document_type"):
                m["columns"][f] = docs["columns"].get(f, "NULL")
    for rec in ("documents", "stage_runs", "calls", "indexed"):  # the rest the SQL source always reads
        if rec not in mapping:
            mapping[rec] = {"from": f"{EMPTY} {ALIAS[rec]}", "columns": _nulls(rec)}
    return mapping, report


def _nulls(rec: str) -> dict:
    """Every field the SQL source reads for this record type, none of them mapped yet."""
    from assay.sources.sql import DEFAULT_MAPPING
    spec = DEFAULT_MAPPING.get(rec) or {}
    return {f: "NULL" for f in [*(spec.get("columns") or {}), *RECORDS[rec][1]]}


def check_mapping(engine, mapping: dict) -> Dict[str, Dict[str, Optional[str]]]:
    from assay.sources.sql import SQLSource, load_mapping
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(mapping, f)
    try:
        return SQLSource("", engine=engine, mapping=load_mapping(f.name)).check()
    finally:
        os.unlink(f.name)


# ---------- what was recorded ----------

def recorded(root: Path, days: float = 30) -> Optional[dict]:
    """The pipeline in what the SDK recorded locally (.assay/events.jsonl, or ASSAY_PATH)."""
    from datetime import datetime, timedelta
    from assay import local, runner, store, workflow
    from assay.models import Window
    from assay.sources.events import EventsSource
    path = Path(os.environ.get("ASSAY_PATH") or root / ".assay" / "events.jsonl")
    if not path.exists():
        return None
    engine = store.make_engine("sqlite://")
    store.metadata.create_all(engine)
    by_type, bad = local.load_file(engine, str(path), "local")
    now = datetime.utcnow()
    graph = workflow.build(runner.CachedSource(EventsSource(engine, "local")), Window(now - timedelta(days=days), now + timedelta(minutes=5)),
                           engine, "events:local")
    return {"path": str(path), "by_type": by_type, "bad": bad, "graph": graph}


def main_path(graph: dict) -> List[dict]:
    by = {n["id"]: n for n in graph["nodes"]}
    out, seen, cur = [], set(), next((n for n in graph["nodes"] if n.get("kind") == "start"), None)
    while cur and cur["id"] not in seen:
        seen.add(cur["id"])
        out.append(cur)
        nx = sorted((e for e in graph["edges"] if e["from"] == cur["id"] and e["to"] not in seen),
                    key=lambda e: -e["documents"])
        cur = by.get(nx[0]["to"]) if nx else None
    return out


# ---------- the command ----------

def _p(text: str, color: str) -> str:
    from assay.local import _paint
    return _paint(text, color)


def _db_url(given: Optional[str]) -> Optional[str]:
    return given or os.environ.get("ASSAY_SOURCE_URL") or os.environ.get("DATABASE_URL")


def overview(root: Path) -> int:
    """`assay connect`: what's here, and the ways in, the least work first."""
    f = scan_code(root)
    url = _db_url(None)
    otel_env = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    server = os.environ.get("ASSAY_URL")
    print(_p(f"Connecting {root.resolve().name} to Assay", "bold"), "\n")
    fw = ", ".join(f"{k} ({len(v)} file{'s' * (len(v) != 1)})" for k, v in sorted(f.frameworks.items())) or "no AI frameworks found"
    rows = [("Code", f"{f.files} Python file{'s' * (f.files != 1)} · {fw}" if f.files else "no Python files here"),
            ("Model calls", f"{len(f.llm_calls)} call site{'s' * (len(f.llm_calls) != 1)}"
             + (f" · a graph with {len(f.graph_nodes)} nodes" if f.graph_nodes else "")
             + (f" · {len(f.tools)} tool{'s' * (len(f.tools) != 1)}" if f.tools else "")),
            ("Tracing", f"OpenTelemetry in {len(f.otel)} file(s)" + (f" · exporting to {otel_env}" if otel_env else "")
             if f.otel or otel_env else "none"),
            ("Database", f"{'ASSAY_SOURCE_URL' if os.environ.get('ASSAY_SOURCE_URL') else 'DATABASE_URL'} is set "
                         f"({url.split(':', 1)[0].split('+')[0]})" if url else "none set (ASSAY_SOURCE_URL or DATABASE_URL)"),
            ("Assay", f"already attached in {len(f.attached)} file(s)" if f.attached else "not attached yet"),
            ("Sends to", server or "this machine: .assay/events.jsonl (set ASSAY_URL and ASSAY_KEY for a server)")]
    w = max(len(k) for k, _ in rows)
    for k, v in rows:
        print(f"  {k:<{w}}  {v}")
    ways = []
    if url:
        from assay import credentials
        login = None if re.search(r"//[^:/@]+:[^@]+@", url) else credentials.suggest(url)
        ways.append((f"Read your database with your {login} login: no password to store, no code changes." if login
                     else "Read your database. No code changes.", "assay connect db"))
    if f.otel or otel_env:
        ways.append(("Send your traces: point an OpenTelemetry collector's otlphttp exporter (encoding: json) at "
                     f"{(server or 'https://<assay>').rstrip('/')}/v1/otlp.", None))
    if f.llm_calls or f.graph_nodes or f.tools:
        _, notes = propose(f)
        if notes:
            ways.append((f"Add a few lines to your code ({len(notes)} change{'s' * (len(notes) != 1)}, shown as a diff first).",
                         "assay connect code"))
    if not ways:
        ways.append(("Record from your code: `with assay.run(\"task\") as run:` and `@assay.step`, or test with pytest --assay.",
                     "assay init"))
    print("\n" + _p("Ways in, the least work first:", "bold"))
    for i, (what, cmd) in enumerate(ways, 1):
        print(f"  {i}. {what}" + (f"\n     {_p('$ ' + cmd, 'green')}" if cmd else ""))
    print(f"\nThen check it: {_p('$ assay connect verify', 'green')}")
    return 0


def db(root: Path, url: Optional[str], out: Optional[str], force: bool = False,
       password_command: Optional[str] = None, preset: Optional[str] = None, profile: Optional[str] = None) -> int:
    """`assay connect db`: read the schema, write the mapping, test it against the database. With a
    login instead of a password: --password-command, or --preset for the cloud's (assay/credentials.py)."""
    import sys
    from assay import credentials
    url = _db_url(url)
    if not url:
        print("Give the database: assay connect db postgresql+psycopg://readonly:…@host/db "
              "(or set ASSAY_SOURCE_URL). Use a read-only login.", file=sys.stderr)
        return 2
    password_command = password_command or os.environ.get(credentials.ENV)
    notes: List[str] = []
    has_password = bool(re.search(r"//[^:/@]+:[^@]+@", url))
    if not password_command and not preset and not has_password:
        preset = credentials.suggest(url)
        if preset:
            print(_p(f"No password in the URL: using your {preset} login (--preset {preset}).", "dim"))
    if preset:
        try:
            got = credentials.preset(preset, url, profile)
        except credentials.CredentialError as exc:
            print(exc, file=sys.stderr)
            return 2
        password_command, url, notes = got["command"], got["url"], got["notes"]
    if password_command:
        print(_p(f"Getting a short-lived password with: {password_command}", "dim"))
    try:
        engine = credentials.engine(url, password_command)
        mapping, report = map_database(engine)
    except credentials.CredentialError as exc:
        print(exc, file=sys.stderr)
        return 2
    except ModuleNotFoundError as exc:  # the database's driver isn't installed
        drivers = {"psycopg": "pip install 'assay-server[postgres]'", "psycopg2": "pip install psycopg2-binary",
                   "pymysql": "pip install pymysql", "snowflake": "pip install snowflake-sqlalchemy",
                   "pyodbc": "pip install pyodbc"}
        name = (exc.name or "").split(".")[0]
        print(f"The driver for this database isn't installed ({name}): {drivers.get(name, 'pip install ' + name)}",
              file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Couldn't read the database's schema: {str(getattr(exc, 'orig', exc)).splitlines()[0]}", file=sys.stderr)
        for n in notes:
            print(f"  note: {n}", file=sys.stderr)
        return 2
    name = re.sub(r"[^\w-]", "_", Path(url.rsplit("/", 1)[-1].split("?")[0] or "pipeline").stem) or "pipeline"
    path = Path(out) if out else root / "mappings" / f"{name}.json"
    shown = os.path.relpath(path) if path.is_absolute() else str(path)
    safe_url = re.sub(r"//([^:/@]+):[^@]*@", r"//\1:…@", url)  # never print a password
    if path.exists() and not force:
        print(f"{path} is already there; nothing written. Use --out for another file, or --force.", file=sys.stderr)
        return 2
    problems = check_mapping(engine, mapping)
    broken = [(rec, fld, err) for rec, fs in problems.items() for fld, err in fs.items() if err]
    for rec, fld, _ in broken:  # a guess that doesn't work against the database isn't kept
        mapping[rec]["columns"][fld] = "NULL"
    print(_p("Your database, as Assay will read it (read-only):", "bold"), "\n")
    for r in report:
        rec = r["record"]
        if not r["table"]:
            print(f"  {_p('–', 'dim')} {rec:<11} not found" + _p(
                {"reviews": "  (people cost: optional)", "errors": "  (reported errors: optional)"}.get(rec, "  (its measures will say unmeasured)"), "dim"))
            continue
        fs = r["fields"]
        got = sum(1 for p in fs.values() if p)
        print(f"  {_p('✓', 'green')} {rec:<11} ← {r['table']}   {got} of {len(fs)} fields")
    guessed = defaultdict(list)
    for r in report:
        for f, p in (r.get("fields") or {}).items():
            if p and p[1] == "guessed":
                guessed[r["record"]].append(f"{f} = {p[0]}")
    if guessed:
        print("\n" + _p("Guessed from the column names, so check these:", "yellow"))
        for rec, items in guessed.items():
            print(f"  {rec:<11} {', '.join(items)}")
    if broken:
        print("\n" + _p("Didn't work against the database, so left out:", "yellow"))
        for rec, f, err in broken:
            print(f"  {rec}.{f}: {err}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(mapping, indent=2) + "\n")
    print(f"\nWrote {shown}. Every field it maps works against the database." if not broken else f"\nWrote {shown}.")
    print("\n" + _p("Next:", "bold"))
    print(f"  {_p(f'$ export ASSAY_SOURCE_URL={safe_url}', 'green')}   (a read-only login)")
    if password_command:
        print(f"  {_p(f'$ export ASSAY_SOURCE_PASSWORD_COMMAND={shlex.quote(password_command)}', 'green')}"
              "   (run for each connection; nothing is stored)")
    for n in notes:
        print(_p(f"  note: {n}", "dim"))
    print(f"  {_p(f'$ export ASSAY_SOURCE_MAPPING={shown}', 'green')}")
    print(f"  {_p('$ assay serve --source sql --every 60', 'green')}   (then open the Workflow page)")
    return 0


def code(root: Path, apply: bool = False) -> int:
    """`assay connect code`: the smallest change, as a diff; written only with --apply."""
    import sys
    f = scan_code(root)
    if not f.files:
        print(f"No Python files in {root}.", file=sys.stderr)
        return 2
    changes, notes = propose(f)
    if not changes:
        print("Nothing to add: " + ("Assay is already attached." if f.attached else
                                    "no model calls, graph nodes or tools found. Wrap your entry point in "
                                    "`with assay.run(\"task\") as run:` and mark steps with @assay.step."))
        return 0
    d = diff(f, changes)
    for line in d.splitlines():
        print(_p(line, "green") if line.startswith("+") and not line.startswith("+++") else
              _p(line, "red") if line.startswith("-") and not line.startswith("---") else line)
    patch = root / ".assay" / "connect.patch" if root.is_dir() else Path(".assay") / "connect.patch"
    patch.parent.mkdir(parents=True, exist_ok=True)
    patch.write_text(d)
    patch = Path(os.path.relpath(patch)) if patch.is_absolute() else patch
    print("\n" + _p(f"{len(notes)} change{'s' * (len(notes) != 1)} in {len(changes)} file{'s' * (len(changes) != 1)}:", "bold"))
    for n in notes:
        print(f"  {n}")
    if not apply:
        print(f"\nNothing is changed yet. The diff is in {patch}.")
        print(f"  {_p('$ assay connect code --apply', 'green')}   or   {_p(f'$ git apply {patch}', 'green')}")
        print("Needs the SDK in your app's environment: pip install assay-evals")
        return 0
    for rel, text in changes.items():
        Path(rel).write_text(text)
    print(f"\nApplied to {len(changes)} file{'s' * (len(changes) != 1)}. Run your app (or its tests) once, then:")
    print(f"  {_p('$ assay connect verify', 'green')}")
    return 0


def verify(root: Path) -> int:
    """`assay connect verify`: the pipeline Assay found in what was recorded, and what's still dark."""
    import sys
    server = os.environ.get("ASSAY_URL")
    graph, where = None, None
    if server:
        import urllib.request
        from urllib.parse import urlencode
        key = os.environ.get("ASSAY_KEY")
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            req = urllib.request.Request(f"{server.rstrip('/')}/v1/sources", headers=headers)
            sources = json.load(urllib.request.urlopen(req, timeout=20))["configured"]
            source = next((s for s in sources if s.startswith("events:")), None)
            if source:
                req = urllib.request.Request(f"{server.rstrip('/')}/v1/workflow?{urlencode({'source': source, 'days': 7})}",
                                             headers=headers)
                graph, where = json.load(urllib.request.urlopen(req, timeout=60)), f"{server} ({source})"
        except Exception as exc:
            print(f"Couldn't read from {server}: {exc}", file=sys.stderr)
            return 2
    else:
        rec = recorded(root)
        if rec:
            graph, where = rec["graph"], rec["path"]
    if not graph or len(graph.get("nodes", [])) <= 2:
        print("Nothing recorded yet. Run your app, or its tests, once, then run this again." +
              ("" if server else " (Recording to .assay/events.jsonl; set ASSAY_URL to send to a server.)"))
        return 1
    path = main_path(graph)
    steps = [n for n in graph["nodes"] if n.get("kind") not in ("start", "end")]
    print(_p("Assay sees your pipeline:", "bold"), _p(f"from {where}", "dim"), "\n")
    print("  " + " → ".join(("Received" if n.get("kind") == "start" else "Done" if n.get("kind") == "end" else n["id"])
                            for n in path))
    print(_p(f"\n  {graph.get('documents', 0):,} runs · {len(steps)} steps", "dim"))
    side = [n["id"] for n in steps if n["id"] not in {p["id"] for p in path}]
    if side:
        print(_p(f"  off the main path: {', '.join(side)}", "dim"))
    code_steps = scan_code(root).steps_in_code
    seen = {n["id"] for n in steps}
    dark = sorted(code_steps - seen)
    if dark:
        print("\n" + _p("In your code, not seen yet:", "yellow"), ", ".join(dark))
        print(_p("  They run on other paths, or the run that would reach them hasn't happened yet.", "dim"))
    stubs = [n["id"] for n in steps if (n.get("noop_rate") or 0) > 0.5]
    if stubs:
        print(_p("Report success but do no work:", "yellow"), ", ".join(stubs))
    print("\nSee it drawn, with where Assay's checks attach, on the Workflow page:")
    if server:
        print(f"  {server.rstrip('/')}/#workflow")
    else:
        print(f"  {_p('$ export ASSAY_STORE_URL=sqlite:///.assay/assay.db', 'green')}")
        print(f"  {_p('$ assay load && assay serve', 'green')}   then open http://127.0.0.1:8400/?source=events:local#workflow")
    return 0
