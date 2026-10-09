"""`assay connect` (assay/attach.py): attach Assay to a pipeline with the least work."""
import os
import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from assay import attach
from assay.__main__ import main

REPO = Path(__file__).resolve().parents[1]
SDK = str(REPO / "sdk" / "python")

GRAPH = '''"""Invoice pipeline."""
from langgraph.graph import StateGraph
import anthropic

client = anthropic.Anthropic()


def classify(state):
    msg = client.messages.create(model="claude-opus-5", max_tokens=200, messages=[])
    return {"doc_type": msg.content[0].text}


def extract(state):
    msg = client.messages.create(model="claude-opus-5", max_tokens=800, messages=[])
    return {"total": msg.content[0].text}


builder = StateGraph(dict)
builder.add_node("classify", classify)
builder.add_node("extract", extract)
graph = builder.compile()
'''
MAIN = '''import sys

from app.graph import graph


def run(text):
    return graph.invoke({"text": text})


if __name__ == "__main__":
    print(run(sys.argv[1]))
'''
TOOLS = '''from langchain_core.tools import tool


@tool
def lookup_vendor(name: str) -> dict:
    """Find a vendor."""
    return {"vendor": name}
'''
STUBS = {  # just enough of each framework for the app to run in a test
    "langgraph/__init__.py": "",
    "langgraph/graph.py": "class StateGraph:\n    def __init__(self, s): self.nodes = []\n"
                          "    def add_node(self, n, fn): self.nodes.append(fn)\n    def compile(self): return self\n"
                          "    def invoke(self, s):\n        for fn in self.nodes: s = {**s, **fn(s)}\n        return s\n",
    "anthropic/__init__.py": "from anthropic.resources.messages import Messages\n"
                             "class Anthropic:\n    def __init__(self): self.messages = Messages()\n",
    "anthropic/resources/__init__.py": "",
    "anthropic/resources/messages.py": "from types import SimpleNamespace as N\nclass Messages:\n    def create(self, **kw):\n"
                                       "        return N(model=kw['model'], usage=N(input_tokens=100, output_tokens=5),"
                                       " content=[N(type='text', text='27.61')])\n",
    "langchain_core/__init__.py": "", "langchain_core/tools.py": "def tool(fn): return fn\n",
}


@pytest.fixture
def project(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "__init__.py").write_text("", encoding="utf-8")
    for name, text in (("graph.py", GRAPH), ("main.py", MAIN), ("tools.py", TOOLS)):
        (tmp_path / "app" / name).write_text(text, encoding="utf-8")
    for name, text in STUBS.items():
        (tmp_path / "stubs" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "stubs" / name).write_text(text, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for k in ("ASSAY_URL", "ASSAY_PATH", "DATABASE_URL", "ASSAY_SOURCE_URL", "OTEL_EXPORTER_OTLP_ENDPOINT"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NO_COLOR", "1")
    return tmp_path


def test_the_scan_finds_the_frameworks_the_model_calls_the_graph_and_the_tools(project):
    f = attach.scan_code(project / "app")
    assert f.files == 4 and set(f.frameworks) == {"Anthropic SDK", "LangGraph", "LangChain"}
    assert sorted(fn for _, _, fn in f.llm_calls) == ["classify", "extract"]
    assert [(n, fn) for _, _, n, fn in f.graph_nodes] == [("classify", "classify"), ("extract", "extract")]
    assert [fn for _, _, fn in f.tools] == ["lookup_vendor"] and [fn for _, _, fn in f.invokes] == ["run"]


def test_the_overview_puts_the_least_work_first(project, capsys, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite:///pipeline.db")
    assert main(["connect"]) == 0
    out = capsys.readouterr().out
    assert "1. Read your database. No code changes." in out and "2. Add a few lines to your code" in out
    assert out.index("assay connect db") < out.index("assay connect code")


def test_code_is_a_diff_until_applied_and_then_the_pipeline_shows_up(project, capsys):
    assert main(["connect", "code"]) == 0
    out = capsys.readouterr().out
    for line in ('+@assay.step("classify")', '+@assay.pipeline("run")', "+@assay.tool", "+assay.instrument()"):
        assert line in out
    assert "Nothing is changed yet" in out and "assay" not in (project / "app" / "graph.py").read_text(encoding="utf-8")
    patch = (project / ".assay" / "connect.patch").read_text(encoding="utf-8")
    assert patch.startswith("--- a/app/graph.py")
    assert main(["connect", "code", "--apply"]) == 0
    graph = (project / "app" / "graph.py").read_text(encoding="utf-8")
    assert '@assay.step("classify")\ndef classify(state):' in graph
    assert "@tool\n@assay.tool\ndef lookup_vendor" in (project / "app" / "tools.py").read_text(encoding="utf-8")
    main_py = (project / "app" / "main.py").read_text(encoding="utf-8")
    assert main_py.index("import assay_sdk as assay") < main_py.index("assay.init()") < main_py.index('@assay.pipeline("run")')
    assert main(["connect", "code"]) == 0 and "Nothing to add: Assay is already attached." in capsys.readouterr().out

    env = {"PYTHONPATH": os.pathsep.join([str(project / "stubs"), str(project), SDK]), "PATH": os.defpath,
           **{k: os.environ[k] for k in ("SYSTEMROOT",) if k in os.environ}}  # Windows' Python needs SYSTEMROOT
    for i in range(2):  # the app runs as before, and records
        r = subprocess.run([sys.executable, "-m", "app.main", f"Invoice {i}"], cwd=project, env=env,
                           capture_output=True, text=True, encoding="utf-8", errors="replace")
        assert r.returncode == 0 and "27.61" in r.stdout, r.stderr
    capsys.readouterr()
    assert main(["connect", "verify"]) == 0
    out = capsys.readouterr().out
    assert "Received → classify → extract → Done" in out and "2 runs · 2 steps" in out
    events = [json.loads(x) for x in (project / ".assay" / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    llm = [e for e in events if e.get("kind") == "llm"]
    assert len(llm) == 4 and {e["name"] for e in llm} == {"classify", "extract"}  # each call, in its step


def test_verify_before_anything_ran(project, capsys):
    assert main(["connect", "verify"]) == 1
    assert "Nothing recorded yet" in capsys.readouterr().out


def test_db_reads_the_schema_writes_the_mapping_and_it_works(project, capsys):
    db = project / "pipeline.db"
    c = sqlite3.connect(db)
    c.executescript("""
      create table documents (id text, created_at timestamp, finished_at timestamp, state text, customer_id text);
      create table stage_executions (id integer, document_id text, step_name text, state text, started_at timestamp,
                                     ended_at timestamp);
      create table llm_calls (id integer, document_id text, stage text, created_at timestamp, model text, cost real);
      create table users (id integer, email text);""")
    now = datetime.utcnow()
    for i in range(4):
        t = now - timedelta(hours=1, minutes=i)
        c.execute("insert into documents values (?,?,?,?,?)", (f"d{i}", t, t + timedelta(minutes=3), "done", "acme"))
        for k, step in enumerate(("ocr", "extract", "publish")):
            c.execute("insert into stage_executions values (?,?,?,?,?,?)", (k, f"d{i}", step, "success",
                                                                         t + timedelta(seconds=k), t + timedelta(seconds=k + 1)))
        c.execute("insert into llm_calls values (?,?,?,?,?,?)", (i, f"d{i}", "extract", t, "claude-opus-5", 0.01))
    c.commit()
    assert main(["connect", "db", f"sqlite:///{db}"]) == 0
    out = capsys.readouterr().out
    assert "✓ stage_runs  ← stage_executions" in out and "– indexed     not found" in out
    assert "stage = step_name" in out and "segment = customer_id" in out  # guesses are shown to check
    assert "Every field it maps works against the database." in out
    mapping = json.loads((project / "mappings" / "pipeline.json").read_text(encoding="utf-8"))
    assert mapping["stage_runs"]["columns"]["stage"] == "s.step_name"
    assert "LEFT JOIN documents d" in mapping["calls"]["from"]
    assert main(["connect", "db", f"sqlite:///{db}"]) == 2  # never overwrites without --force

    from assay import runner, workflow
    from assay.models import Window
    from assay.sources.sql import SQLSource, load_mapping
    src = SQLSource(f"sqlite:///{db}", mapping=load_mapping(str(project / "mappings" / "pipeline.json")))
    g = workflow.build(runner.CachedSource(src), Window(now - timedelta(days=1), now + timedelta(minutes=1)))
    assert [n["id"] for n in attach.main_path(g)] == ["(received)", "ocr", "extract", "publish", "(done)"]
    assert len(list(src.calls(Window(now - timedelta(days=1), now)))) == 4
