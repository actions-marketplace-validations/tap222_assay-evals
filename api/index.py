"""Vercel entry point. Vercel's Python runtime serves the ASGI `app` below.

Vercel functions have no persistent disk and no long-running process, so:
- without ASSAY_STORE_URL, results go to SQLite in /tmp and the demo tenant
  is loaded on each fresh instance (fine for a preview, lost on redeploy);
- set ASSAY_STORE_URL to a Postgres URL (e.g. Neon) for data that persists;
- the in-process scheduler is off; Vercel Cron calls /v1/cron instead.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

if not os.environ.get("ASSAY_STORE_URL"):
    os.environ["ASSAY_STORE_URL"] = "sqlite:////tmp/assay.db"
    os.environ.setdefault("ASSAY_AUTO_DEMO", "1")
os.environ["ASSAY_SCHEDULE_MINUTES"] = "0"

from assay.api import create_app  # noqa: E402

app = create_app()
