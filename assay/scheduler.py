"""Built-in scheduler: run measures for each configured source every N minutes.

Good enough for a single instance. With several replicas, run it on one only
(or use Celery Beat / cron with `assay run`) so runs aren't duplicated.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from typing import Dict, Optional

from sqlalchemy.engine import Engine

from assay import runner
from assay.config import Settings

log = logging.getLogger(__name__)


class Scheduler:
    def __init__(self, engine: Engine, settings: Settings):
        self.engine, self.settings = engine, settings
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._sweeper: Optional[threading.Thread] = None
        self.last: Dict[str, dict] = {}
        self.last_sweep: Optional[dict] = None
        self.reviewed: Dict[str, str] = {}  # source: the day its conversations were last read
        self.monitored: Dict[str, datetime] = {}  # source: when the sampled judge last ran

    @property
    def enabled(self) -> bool:
        return self.settings.schedule_minutes > 0 and bool(self.settings.schedule_sources)

    def start(self) -> None:
        if self.enabled and not self._thread:
            self._thread = threading.Thread(target=self._loop, name="assay-scheduler", daemon=True)
            self._thread.start()
        if self.settings.evaluate_seconds > 0 and not self._sweeper:
            self._sweeper = threading.Thread(target=self._sweep_loop, name="assay-lifecycle", daemon=True)
            self._sweeper.start()

    def sweep(self) -> dict:
        """Mark quiet agent runs abandoned and evaluate every run that ended (assay/lifecycle.py)."""
        from assay import lifecycle
        try:
            out = lifecycle.sweep(self.engine, self.settings.abandon_minutes)
            self.last_sweep = {"at": datetime.utcnow().isoformat(), "ok": True, **out}
        except Exception as exc:
            log.exception("Lifecycle sweep failed")
            self.last_sweep = {"at": datetime.utcnow().isoformat(), "ok": False, "error": str(exc)}
        try:  # even when the sweep failed: a stuck evaluation is what this is for
            self.last_sweep["backlog"] = lifecycle.check_backlog(self.engine, self.settings.backlog_minutes)
        except Exception:
            log.exception("Checking the evaluation backlog failed")
        return self.last_sweep

    def _sweep_loop(self) -> None:
        while not self._stop.is_set():
            self.sweep()
            self._stop.wait(self.settings.evaluate_seconds)

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> None:
        for name in self.settings.schedule_sources:
            started = datetime.utcnow()
            try:
                source = runner.resolve_source(name, self.engine, self.settings)
                run_id = runner.run_measures(
                    self.engine, source, runner.window_for_days(self.settings.schedule_window_days),
                    notify=_notifier(self.engine, name, self.settings), alert_min_n=self.settings.alert_min_n,
                    alert_after_runs=self.settings.alert_after_runs)
                self.last[name] = {"at": started.isoformat(), "ok": True, "run_id": run_id}
                self._review(name, source, started)
                self._monitor(name, started)
            except Exception as exc:
                log.exception("Scheduled run failed for %s", name)
                self.last[name] = {"at": started.isoformat(), "ok": False, "error": str(exc)}

    def _review(self, name: str, source, started: datetime) -> None:
        """Once a day, per events source: read a sample of its conversations (assay/review.py). Opt-in
        (ASSAY_REVIEW_DAILY): it costs a model call per conversation read."""
        day = started.strftime("%Y-%m-%d")
        if not self.settings.review_daily or not name.startswith("events:") or self.reviewed.get(name) == day:
            return
        from assay import review
        try:
            out = review.run(self.engine, source, name.split(":", 1)[1], review.reader_for(self.settings),
                             n=self.settings.review_sample, rt=review.runtime_for(self.settings),
                             redact=self.settings.judge_redact)
            self.last[name]["review"] = {k: out[k] for k in ("read", "went_wrong", "new_categories")}
        except Exception:
            log.exception("The daily review failed for %s", name)
        self.reviewed[name] = day

    def _monitor(self, name: str, started: datetime) -> None:
        """Each run, per events source: the sampled judge on production runs that ended since the last
        one (within the day's budget), then quality alerts against their targets (assay/monitor.py)."""
        if not name.startswith("events:"):
            return
        from assay import monitor
        tenant = name.split(":", 1)[1]
        try:
            if self.settings.production_judge_sample:
                since = self.monitored.get(name) or started - timedelta(minutes=max(self.settings.schedule_minutes, 60))
                self.last[name]["production_judge"] = {k: v for k, v in monitor.judge_window(
                    self.engine, tenant, self.settings, since, started).items() if k != "summary"}
                self.monitored[name] = started
            monitor.alert(self.engine, tenant, monitor.quality(self.engine, tenant, 7, started), started)
        except Exception:
            log.exception("Production monitoring failed for %s", name)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.settings.schedule_minutes * 60)

    def status(self) -> dict:
        return {"enabled": self.enabled, "every_minutes": self.settings.schedule_minutes,
                "window_days": self.settings.schedule_window_days,
                "sources": self.settings.schedule_sources, "last": self.last,
                "lifecycle": {"every_seconds": self.settings.evaluate_seconds,
                              "abandon_minutes": self.settings.abandon_minutes, "last": self.last_sweep}}


def _notifier(engine, source_name, settings):
    from assay import integrations
    return integrations.notifier(engine, source_name, settings.public_url, settings.notifier())
