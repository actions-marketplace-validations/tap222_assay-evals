"""Command line: python -m assay <command>"""
from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from assay import runner, store
from assay.config import Settings


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="assay", description="Evaluation and observability for document-AI pipelines.")
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
    r.add_argument("--source", default="docai_core", help="docai_core or events:<tenant>")
    r.add_argument("--days", type=int, default=7)

    sub.add_parser("check-source", help="Show which mapped DocAI Core columns exist")
    sub.add_parser("demo", help="Load a synthetic demo tenant and backfill 7 weeks of daily runs")

    args = p.parse_args(argv)
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

    if args.cmd == "run":
        try:
            source = runner.resolve_source(args.source, engine, settings)
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        run_id = runner.run_measures(engine, source, runner.window_for_days(args.days),
                                     notify=settings.notifier(), alert_min_n=settings.alert_min_n,
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

    if args.cmd == "check-source":
        if not settings.docai_url:
            print("Set ASSAY_DOCAI_URL first.", file=sys.stderr)
            return 2
        from assay.sources.docai_core import DocAICoreSource
        report = DocAICoreSource(settings.docai_url).check()
        missing = {t: [c for c, ok in cols.items() if not ok] for t, cols in report.items()}
        print(json.dumps(report, indent=2))
        if any(missing.values()):
            print("\nNot found (override in ASSAY_DOCAI_MAPPING, or set to \"NULL\"):", file=sys.stderr)
            for t, cols in missing.items():
                for c in cols:
                    print(f"  {t}.{c}", file=sys.stderr)
            return 1
        return 0

    if args.cmd == "demo":
        from assay.demo import seed
        print(json.dumps(seed(engine), indent=2))
        print("Demo loaded. Start the dashboard with: python -m assay serve")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
