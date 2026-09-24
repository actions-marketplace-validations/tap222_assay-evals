from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _list(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


@dataclass
class Settings:
    store_url: str = "sqlite:///./assay.db"
    source_url: Optional[str] = None  # the pipeline database, read-only
    downstream_url: Optional[str] = None
    downstream_hash_sql: Optional[str] = None
    api_key: Optional[str] = None

    # alerting
    webhook_url: Optional[str] = None
    webhook_format: str = "slack"  # slack | json
    public_url: Optional[str] = None  # used for links in alert messages
    alert_min_n: int = 30  # slices smaller than this are never alerted on
    alert_after_runs: int = 2  # consecutive runs a condition must hold before it notifies

    # Seed the synthetic demo tenant on the first request if the store is empty.
    # Meant for throwaway hosts (a Vercel preview with SQLite in /tmp).
    auto_demo: bool = False
    # Vercel Cron sends "Authorization: Bearer $CRON_SECRET" to /v1/cron.
    cron_secret: Optional[str] = None

    # built-in scheduler (0 = off)
    schedule_minutes: int = 0
    schedule_sources: List[str] = field(default_factory=list)
    schedule_window_days: float = 1.0

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(
            store_url=e("ASSAY_STORE_URL", cls.store_url),
            source_url=e("ASSAY_SOURCE_URL"),
            downstream_url=e("ASSAY_DOWNSTREAM_URL"),
            downstream_hash_sql=e("ASSAY_DOWNSTREAM_HASH_SQL"),
            api_key=e("ASSAY_API_KEY"),
            webhook_url=e("ASSAY_WEBHOOK_URL"),
            webhook_format=e("ASSAY_WEBHOOK_FORMAT", "slack"),
            public_url=e("ASSAY_PUBLIC_URL"),
            alert_min_n=int(e("ASSAY_ALERT_MIN_N", "30")),
            alert_after_runs=int(e("ASSAY_ALERT_AFTER_RUNS", "2")),
            schedule_minutes=int(e("ASSAY_SCHEDULE_MINUTES", "0")),
            schedule_sources=_list(e("ASSAY_SCHEDULE_SOURCES")),
            schedule_window_days=float(e("ASSAY_SCHEDULE_WINDOW_DAYS", "1")),
            auto_demo=e("ASSAY_AUTO_DEMO", "").lower() in ("1", "true", "yes"),
            cron_secret=e("CRON_SECRET"),
        )

    def notifier(self):
        if not self.webhook_url:
            return None
        from assay.alerts import webhook_notifier
        return webhook_notifier(self.webhook_url, self.webhook_format, self.public_url)
