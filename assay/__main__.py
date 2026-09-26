"""Command line: python -m assay <command>"""
from __future__ import annotations

import argparse
import json
import os
import sys

from sqlalchemy import select

from assay import runner, store
from assay.config import Settings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="assay", description="Evaluation and observability for document-intelligence pipelines.")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Run the API and dashboard")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8400)
    s.add_argument("--every", type=int, metavar="MINUTES",
                   help="Also run measures every N minutes (overrides ASSAY_SCHEDULE_MINUTES)")
    s.add_argument("--source", action="append", dest="sources", metavar="SOURCE",
                   help="Source to schedule, repeatable (overrides ASSAY_SCHEDULE_SOURCES)")
    s.add_argument("--window-days", type=float, help="Window each scheduled run covers (default 1)")

    r = sub.add_parser("run", help="Compute all measures once and store the results")
    r.add_argument("--source", default="sql", help="sql (your pipeline database) or events:<tenant>")
    r.add_argument("--days", type=int, default=7)

    sub.add_parser("check-source", help="Test every mapped field against your pipeline database")
    cn = sub.add_parser("connect", help="Attach Assay to your pipeline: what's here and the least-work way in; "
                                        "db (read the schema, write the mapping), code (the change, as a diff), "
                                        "verify (the pipeline Assay found)")
    cn.add_argument("what", nargs="?", choices=["db", "code", "verify"], help="Leave out for what's here")
    cn.add_argument("target", nargs="?", help="db: the database URL; code: the folder or file (default: here)")
    cn.add_argument("--apply", action="store_true", help="code: write the change (it's shown first either way)")
    cn.add_argument("--out", help="db: where to write the mapping (default: mappings/<database>.json)")
    cn.add_argument("--force", action="store_true", help="db: overwrite the mapping file")

    k = sub.add_parser("keys", help="Create, list and revoke API keys")
    ks = k.add_subparsers(dest="keys_cmd", required=True)
    kc = ks.add_parser("create", help="Create a key; the secret is printed once")
    kc.add_argument("--tenant", required=True, help="Tenant the key belongs to, or '*' for a platform key")
    kc.add_argument("--scopes", required=True, help="Comma-separated: ingest, read, manage, admin")
    kc.add_argument("--name", required=True, help="What uses it, e.g. 'invoice pipeline (prod)'")
    kc.add_argument("--expires-in-days", type=int)
    kl = ks.add_parser("list", help="List keys (never shows secrets)")
    kl.add_argument("--tenant")
    kr = ks.add_parser("revoke", help="Revoke a key immediately")
    kr.add_argument("id", type=int)

    b = sub.add_parser("backfill", help="Replay past days so baselines and alerts work from day one")
    b.add_argument("--source", default="sql", help="sql or events:<tenant>")
    b.add_argument("--days", type=int, default=30)
    b.add_argument("--window-days", type=float, default=1.0)

    c = sub.add_parser("coverage", help="Which measures your data can answer, and what would unlock the rest")
    c.add_argument("--source", default="sql", help="sql or events:<tenant>")
    c.add_argument("--days", type=float, default=7)
    sub.add_parser("demo", help="Load a synthetic demo tenant and backfill 7 weeks of daily runs")
    ld = sub.add_parser("load", help="Load events the SDK recorded locally (no server set) into the store")
    ld.add_argument("file", nargs="?", default=".assay/events.jsonl")
    ld.add_argument("--tenant", default="local", help="Tenant to load them into (default: local)")
    sub.add_parser("init", help="Set up local testing here: assay.toml and a runnable example")
    t = sub.add_parser("test", help="Run your tests with the SDK recording, check every run, compare with the "
                                    "last run that passed")
    t.add_argument("--repeat", type=int, metavar="N", help="Attempts per case (overrides assay.toml)")
    t.add_argument("--baseline", metavar="RUN", help="Compare with this run instead of the last that passed; "
                                                     "'none' for no baseline")
    t.add_argument("--upload", action="store_true", help="Also send the run to a server (ASSAY_URL, ASSAY_KEY)")
    t.add_argument("--junit", metavar="PATH", help="Also write JUnit XML, for CI to show each case")
    t.add_argument("--timeout", type=float, metavar="SECONDS", help="Stop an attempt that runs longer (overrides "
                                                                     "assay.toml)")
    t.add_argument("--failed", action="store_true", help="Run only the cases that didn't pass last time (pytest)")
    t.add_argument("--judge", action="store_true", help="Also have an LLM judge plan quality and consistency "
                                                        "(needs `pip install anthropic`; a model call per run)")
    t.add_argument("command", nargs=argparse.REMAINDER, help="-- <command> (overrides assay.toml)")
    u = sub.add_parser("upload", help="Send a test run (the latest, by default) to an Assay server")
    u.add_argument("run", nargs="?", help="A run id instead of the latest")
    for q in (t, u):
        q.add_argument("--url", help="Server address (default: ASSAY_URL)")
        q.add_argument("--key", help="API key with the ingest scope (default: ASSAY_KEY)")
        q.add_argument("--tenant", dest="send_tenant", metavar="TENANT",
                       help="Tenant to send to (a tenant key's own is used otherwise)")
    pc = sub.add_parser("pr-comment", help="Post the latest run's summary on the pull request (GitHub Actions), "
                                           "updating Assay's earlier comment")
    pc.add_argument("--summary", default=".assay/summary.md")
    pc.add_argument("--pr", type=int, help="The PR number (default: from the GitHub Actions event)")
    pc.add_argument("--repo", help="owner/name (default: GITHUB_REPOSITORY)")
    d = sub.add_parser("diff", help="What behavior changed between two runs: regressions with the flow before "
                                    "and after, improvements, flaky cases, severity")
    d.add_argument("baseline", nargs="?", help="A run id or a version (default: each case's last passing run)")
    d.add_argument("current", nargs="?", help="A run id or a version (default: the latest run)")
    d.add_argument("--format", choices=["text", "markdown", "json"], default="text")
    a = sub.add_parser("accept", help="Make the latest test run the baseline, known failures and all")
    a.add_argument("run", nargs="?", help="A run id instead of the latest")
    sub.add_parser("schema", help="Print the v1 event schema as JSON Schema")

    args = p.parse_args(argv)
    if args.cmd == "pr-comment":
        from assay import github, local
        repo, pr, token = args.repo or os.environ.get("GITHUB_REPOSITORY"), args.pr or github.pr_number(), \
            os.environ.get("GITHUB_TOKEN")
        if not pr:
            print("Not a pull request: nothing to comment on.")
            return 0
        if not (repo and token):
            print("Set GITHUB_TOKEN and GITHUB_REPOSITORY (GitHub Actions sets the second; pass the first "
                  "from secrets.GITHUB_TOKEN).", file=sys.stderr)
            return 2
        try:
            body = open(args.summary, encoding="utf-8").read()
        except OSError:
            print(f"No summary at {args.summary}: run `pytest --assay` or `assay test` first.", file=sys.stderr)
            return 2
        try:
            print(f"{github.comment(body, repo, pr, token, local.MARKER).capitalize()} the comment on PR #{pr}.")
        except (RuntimeError, OSError) as exc:
            print(exc, file=sys.stderr)
            return 2
        return 0
    if args.cmd == "diff":
        from pathlib import Path
        from assay import diff
        return diff.main(Path.cwd(), args.baseline, args.current, args.format)
    if args.cmd in ("init", "test", "accept", "upload"):
        from pathlib import Path
        from assay import local
        root = Path.cwd()
        if args.cmd == "upload":
            return local.upload(root, args.run, args.url, args.key, args.send_tenant)
        if args.cmd == "accept":
            return local.accept(root, args.run)
        if args.cmd == "test":
            send = {"url": args.url, "key": args.key, "tenant": args.send_tenant} if args.upload else None
            return local.test(root, local.split_command(args.command), args.repeat, args.baseline, send,
                              args.junit, args.timeout, args.failed, args.judge)
        made = local.init(root)
        print(f"Created {', '.join(made)}." if made else f"{local.CONFIG} is already here; nothing changed.")
        try:
            import pytest  # noqa: F401
            print("Next: `pytest --assay tests/ai`. Then add your own tests next to the example.")
        except ImportError:
            print("Next: `pip install pytest`, then `pytest --assay tests/ai`.")
        return 0
    if args.cmd == "schema":
        from assay.schema import json_schema
        print(json.dumps(json_schema(), indent=1))
        return 0
    settings = Settings.from_env()

    if args.cmd == "serve":
        if args.every is not None:
            settings.schedule_minutes = args.every
        if args.sources:
            settings.schedule_sources = args.sources
        if args.window_days is not None:
            settings.schedule_window_days = args.window_days
        import uvicorn
        from assay.api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port)
        return 0

    engine = store.make_engine(settings.store_url)
    from assay import integrations

    if args.cmd == "run":
        try:
            source = runner.resolve_source(args.source, engine, settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        run_id = runner.run_measures(engine, source, runner.window_for_days(args.days),
                                     notify=integrations.notifier(engine, source.name, settings.public_url,
                                                                  settings.notifier()),
                                     alert_min_n=settings.alert_min_n,
                                     alert_after_runs=settings.alert_after_runs)
        out = runner.latest_run(engine, source.name)
        for mid, m in out["measures"].items():
            val = m["overall"]["value"] if m["overall"] else None
            shown = "—" if val is None else f"{val:.4g}"
            print(f"{mid:24} {m['status']:10} {shown:>10}  {m['reason'] or ''}")
        with engine.connect() as conn:
            a = store.alerts
            live = conn.execute(select(a).where((a.c.source == source.name) & (a.c.state == "open"))).all()
        print(f"run {run_id} stored · {len(live)} open alerts")
        for r in live:
            print(f"  [{r.kind}] {r.message}")
        return 0

    if args.cmd == "keys":
        from assay import auth
        if args.keys_cmd == "create":
            try:
                row, secret = auth.create_key(engine, args.tenant, args.name,
                                              [s.strip() for s in args.scopes.split(",") if s.strip()],
                                              args.expires_in_days)
            except ValueError as exc:
                print(exc, file=sys.stderr)
                return 2
            print(f"Created key {row['id']} for tenant {row['tenant']} with scopes {', '.join(row['scopes'])}.")
            print(f"\n  {secret}\n\nStore it now: it isn't shown again. Send it as 'Authorization: Bearer <key>'.")
            print("Authentication is now required on this server." if not settings.admin_key else "")
            return 0
        if args.keys_cmd == "list":
            for r in auth.list_keys(engine, args.tenant):
                state = "revoked" if r["revoked_at"] else "active"
                print(f"{r['id']:>4}  {r['prefix']}…  {r['tenant']:12} {','.join(r['scopes']):22} {state:8} "
                      f"last used {r['last_used_at'] or 'never'}  {r['name']}")
            return 0
        if args.keys_cmd == "revoke":
            ok = auth.revoke_key(engine, args.id)
            print("Revoked." if ok else f"No active key {args.id}.")
            return 0 if ok else 1

    if args.cmd in ("backfill", "coverage"):
        try:
            source = runner.resolve_source(args.source, engine, settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        if args.cmd == "backfill":
            out = runner.backfill(engine, source, args.days, args.window_days,
                                  settings.alert_min_n, settings.alert_after_runs)
            print(f"{out['runs_created']} runs created, {out['skipped']} days already had one.")
            return 0
        from assay.coverage import compute
        rep = compute(source, runner.window_for_days(args.days), runner.load_rates(engine, source.name))
        for name, p in rep["records"].items():
            print(f"{name:11} {'%d rows' % p['rows'] if p['available'] else 'not provided'}")
        print()
        for m in rep["measures"]:
            print(f"{m['status']:8} {m['name']}")
            for f in m["missing"]:
                print(f"         needs {f}")
            for i in m["improve"]:
                print(f"         better with {i['field']}: {i['why']}")
        c = rep["counts"]
        print(f"\n{c['live']} live, {c['partial']} partial, {c['blocked']} blocked")
        return 0

    if args.cmd == "connect":
        from pathlib import Path
        from assay import attach
        root = Path.cwd()
        if args.what == "db":
            return attach.db(root, args.target, args.out, args.force)
        if args.what == "code":
            return attach.code(Path(args.target) if args.target else root, args.apply)
        if args.what == "verify":
            return attach.verify(root)
        return attach.overview(Path(args.target) if args.target else root)

    if args.cmd == "check-source":
        if not settings.source_url:
            print("Set ASSAY_SOURCE_URL first.", file=sys.stderr)
            return 2
        from assay.sources.sql import SQLSource
        report = SQLSource(settings.source_url).check()
        bad = 0
        for table, fields in report.items():
            for field, err in fields.items():
                print(f"{'ok ' if err is None else 'ERR'} {table}.{field}{'' if err is None else '  ' + err}")
                bad += err is not None
        if bad:
            print(f"\n{bad} field(s) failed. Fix them in your mapping file (ASSAY_SOURCE_MAPPING), "
                  "or set them to \"NULL\" if your schema doesn't record them.", file=sys.stderr)
            return 1
        print("\nAll mapped fields work.")
        return 0

    if args.cmd == "load":
        from assay.local import load_file
        try:
            by_type, bad = load_file(engine, args.file, args.tenant)
        except OSError as exc:
            print(f"Can't read {args.file}: {exc.strerror}", file=sys.stderr)
            return 2
        if bad:
            print(f"{len(bad)} bad line(s) in {args.file}; nothing loaded:", *bad[:20], sep="\n  ", file=sys.stderr)
            return 1
        print(f"Loaded {sum(by_type.values())} events into tenant '{args.tenant}': "
              + (", ".join(f"{v} {k}" for k, v in by_type.items()) or "none"))
        print(f"Loading the same file again changes nothing. See them with: python -m assay serve "
              f"(source events:{args.tenant})")
        return 0

    if args.cmd == "demo":
        from assay.demo import seed
        print(json.dumps(seed(engine), indent=2))
        print("Demo loaded. Start the dashboard with: python -m assay serve")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
