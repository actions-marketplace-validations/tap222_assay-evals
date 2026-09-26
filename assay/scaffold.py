"""`assay connect evals`: a proposed test for every model call in the code, so an eval is added with
the call, the way a unit test is added with a function.

For each function that calls a model (Anthropic, OpenAI, Gemini, LiteLLM, ...), a pytest file is
proposed under tests/ai/: the call's facts (model, system prompt, tools, structured output) as its
context, a spec to write, cases to fill in, and the checks that need no judge: output there, the
fields a structured output requires, only the tools it was given. A judge is left for what no rule
can check, commented, with the reminder to calibrate it (assay calibrate) before trusting it.

Proposed, not trusted: every file is skipped until a person writes its spec and cases and removes
the mark. The same AI that wrote the code writing its evals, unreviewed, shares the code's blind
spots; a person deciding what "right" means is the part that can't be generated. Nothing is
written without --apply, and a function that already has a test is left alone.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from assay.attach import LLM_CALLS, _dotted, _py_files

MARK = "proposed by assay connect evals"


@dataclass
class Site:
    file: str
    line: int
    function: str
    module: str
    params: List[str]
    docstring: Optional[str] = None
    model: Optional[str] = None
    system: Optional[str] = None
    tools: List[str] = field(default_factory=list)
    required: List[str] = field(default_factory=list)  # a structured output's required fields
    structured: bool = False
    is_async: bool = False


def _literal(node, consts: Dict[str, ast.AST]):
    """A string, dict or list the code spells out, following a module-level name once."""
    if isinstance(node, ast.Name) and node.id in consts:
        node = consts[node.id]
    try:
        return ast.literal_eval(node)
    except (ValueError, SyntaxError, TypeError):
        return None


def _system(call: ast.Call, consts) -> Optional[str]:
    for k in call.keywords:
        if k.arg in ("system", "instructions", "system_instruction"):
            v = _literal(k.value, consts)
            if isinstance(v, str):
                return v
            if isinstance(v, list) and v and isinstance(v[0], dict):
                return " ".join(str(b.get("text", "")) for b in v)
        if k.arg == "messages":
            v = _literal(k.value, consts)
            if isinstance(v, list):
                for m in v:
                    if isinstance(m, dict) and m.get("role") in ("system", "developer") and isinstance(m.get("content"), str):
                        return m["content"]
    return None


def _required(schema) -> List[str]:
    if isinstance(schema, dict):
        for key in ("schema", "json_schema", "format"):
            if isinstance(schema.get(key), dict):
                inner = _required(schema[key])
                if inner:
                    return inner
        req = schema.get("required")
        if isinstance(req, list):
            return [str(x) for x in req]
    return []


def sites(root: Path) -> List[Site]:
    """Every top-level function that calls a model, with what the call says about itself."""
    out = []
    for path in _py_files(root):
        rel = os.path.relpath(path, Path.cwd()) if path.is_absolute() else str(path)
        try:
            tree = ast.parse(path.read_text())
        except (OSError, SyntaxError, UnicodeDecodeError, ValueError):
            continue
        if Path(rel).name.startswith("test_") or "/tests/" in f"/{rel}":
            continue
        consts = {t.id: n.value for n in tree.body if isinstance(n, ast.Assign) for t in n.targets
                  if isinstance(t, ast.Name)}
        module = rel[:-3].replace(os.sep, ".").replace("/", ".")
        if module.endswith(".__init__"):
            module = module[:-9]
        for fn in tree.body:
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name.startswith("_"):
                continue
            calls = [n for n in ast.walk(fn) if isinstance(n, ast.Call) and (
                any(("." + _dotted(n.func)).endswith(c) for c in LLM_CALLS)
                or _dotted(n.func) in ("litellm.completion", "litellm.acompletion"))]
            if not calls:
                continue
            c = calls[0]
            s = Site(rel, c.lineno, fn.name, module, [a.arg for a in fn.args.args if a.arg not in ("self", "cls")],
                     ast.get_docstring(fn), is_async=isinstance(fn, ast.AsyncFunctionDef))
            for k in c.keywords:
                if k.arg == "model":
                    v = _literal(k.value, consts)
                    s.model = v if isinstance(v, str) else None
                elif k.arg == "tools":
                    v = _literal(k.value, consts)
                    if isinstance(v, list):
                        s.tools = [str(t.get("name") or (t.get("function") or {}).get("name")) for t in v
                                   if isinstance(t, dict) and (t.get("name") or (t.get("function") or {}).get("name"))]
                elif k.arg in ("output_config", "response_format", "response_model", "text_format", "text"):
                    s.structured = True
                    s.required = _required(_literal(k.value, consts))
            s.system = _system(c, consts)
            out.append(s)
    return out


def tested(root: Path, s: Site) -> bool:
    """Whether a test already imports the function (from m import f) or calls it through its module (m.f)."""
    fn, mod = re.escape(s.function), re.escape(s.module.split(".")[-1])
    pattern = re.compile(rf"^\s*from\s+[\w.]+\s+import\s+[^\n]*\b{fn}\b|\b{mod}\.{fn}\s*\(", re.M)
    for path in _py_files(Path.cwd()):  # the project's tests, wherever the code scanned is
        name = Path(path).name
        if name.startswith("test_") or name.endswith("_test.py"):
            try:
                if pattern.search(Path(path).read_text()):
                    return True
            except (OSError, UnicodeDecodeError):
                continue
    return False


def _short(text: Optional[str], n: int = 160) -> str:
    t = re.sub(r"\s+", " ", text or "").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def render(s: Site) -> str:
    facts = [f"model {s.model}" if s.model else "the model it's configured with",
             f'system prompt "{_short(s.system, 120)}"' if s.system else "no system prompt spelled out here",
             f"tools: {', '.join(s.tools)}" if s.tools else "no tools",
             ("structured output" + (f" requiring {', '.join(s.required)}" if s.required else "")) if s.structured
             else "free-text output"]
    args = ", ".join(f'"{p}": None' for p in s.params)  # TODO values
    call = f"{s.function}(**args)"
    checks = ["    assert out not in (None, \"\", [], {}), \"it returned nothing\""]
    if s.required:
        checks += ["    data = out if isinstance(out, dict) else json.loads(out)  # the structured output",
                   f"    for key in {s.required!r}:",
                   "        assert key in data, f\"{key} is missing from the output\""]
    if s.tools:
        checks.append(f"    # the tool it should use, and no more tools than it's given: "
                      f"expect(assay_case).must_call({s.tools[0]!r}).max_tools_exposed({len(s.tools)})")
    lines = [
        f'"""Evals for {s.function} ({s.file}:{s.line}): {MARK}.',
        "",
        f"What {s.function} must do (write this first; it's the spec the cases check):",
        f"  TODO: {_short(s.docstring, 200) if s.docstring else 'what a right answer is, and what must never happen'}",
        "",
        f"Its model call: {'; '.join(facts)}.",
        '"""',
        *(["import json", ""] if s.required else []),
        "import pytest",
        "",
        "# Proposed, not trusted: nothing here runs until a person writes the spec and the cases, and removes",
        "# this mark. Evals the same AI wrote with the code, unreviewed, share the code's blind spots.",
        f'pytestmark = pytest.mark.skip(reason="{MARK}: write the spec and the cases, then remove this mark")',
        "",
        "# Start from real failures: `assay learn` candidates and `assay review` categories are the best cases.",
        "CASES = [",
        f"    pytest.param({{{args}}}, {{\"answer\": None}}, id=\"typical\"),",
        f"    pytest.param({{{args}}}, {{\"answer\": None}}, id=\"edge-case\"),",
        f"    pytest.param({{{args}}}, {{\"answer\": None}}, id=\"a-failure-seen-in-production\"),",
        "]",
        "",
        "",
        '@pytest.mark.parametrize("args, want", CASES)',
        f"{'async ' if s.is_async else ''}def test_{s.function}(assay_case, args, want):",
        f"    from {s.module} import {s.function}  # here, so a proposed file never breaks collection",
        f"    out = {'await ' if s.is_async else ''}{call}  # its model call is recorded: tokens, cost, what went in",
        "    # Checks that need no judge come first:",
        *checks,
        "    if want.get(\"answer\") is not None:",
        "        assert str(want[\"answer\"]).lower() in str(out).lower()",
        "    # A judge only for what no rule can check, and calibrate it (assay calibrate) before trusting it:",
        "    # from assay_sdk import Judge, evaluate",
        "    # evaluate(Judge(\"anthropic\", \"claude-opus-5\"), args, out, schema=VERDICT, run=assay_case, field=\"helpful\")",
        "",
    ]
    if s.is_async:
        lines.insert(lines.index("import pytest") + 1, "")
        lines[lines.index('@pytest.mark.parametrize("args, want", CASES)')] = \
            '@pytest.mark.parametrize("args, want", CASES)\n@pytest.mark.asyncio'
    return "\n".join(lines)


def target(s: Site, folder: str) -> Path:
    return Path(folder) / f"test_{s.module.split('.')[-1]}_{s.function}.py"


def plan(root: Path, folder: str = "tests/ai") -> List[dict]:
    """[{"site", "path", "state": new | exists | tested, "text"}] for every model call site."""
    out = []
    for s in sites(root):
        path = target(s, folder)
        state = "exists" if path.exists() else "tested" if tested(root, s) else "new"
        out.append({"site": s, "path": path, "state": state, "text": render(s) if state == "new" else None})
    return out
