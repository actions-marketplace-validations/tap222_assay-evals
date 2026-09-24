"""Built-in scheduler: run measures for each configured source every N minutes.

Good enough for a single instance. With several replicas, run it on one only
(or use Celery Beat / cron with `assay run`) so runs aren't duplicated.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime
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
        self.last: Dict[str, dict] = {}

    @property
    def enabled(self) -> bool:
        return self.settings.schedule_minutes > 0 and bool(self.settings.schedule_sources)

    def start(self) -> None:
        if self.enabled and not self._thread:
            self._thread = threading.Thread(target=self._loop, name="assay-scheduler", daemon=True)
            self._thread.start()

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
            except Exception as exc:
                log.exception("Scheduled run failed for %s", name)
                self.last[name] = {"at": started.isoformat(), "ok": False, "error": str(exc)}

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.settings.schedule_minutes * 60)

    def status(self) -> dict:
        return {"enabled": self.enabled, "every_minutes": self.settings.schedule_minutes,
                "window_days": self.settings.schedule_window_days,
                "sources": self.settings.schedule_sources, "last": self.last}


def _notifier(engine, source_name, settings):
    from assay import integrations
    return integrations.notifier(engine, source_name, settings.public_url, settings.notifier())
