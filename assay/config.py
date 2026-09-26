from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List, Optional


def _num(v: Optional[str]) -> Optional[float]:
    return float(v) if v not in (None, "") else None


def _list(v: Optional[str]) -> List[str]:
    return [x.strip() for x in (v or "").split(",") if x.strip()]


@dataclass
class Settings:
    store_url: str = "sqlite:///./assay.db"
    source_url: Optional[str] = None  # the pipeline database, read-only
    source_password_command: Optional[str] = None  # a CLI that prints a short-lived password (assay/credentials.py)
    downstream_url: Optional[str] = None
    downstream_hash_sql: Optional[str] = None
    # Platform admin key from the environment: every tenant, every scope. Use it
    # to create the first real keys, then keep it for break-glass use.
    admin_key: Optional[str] = None
    # SSO (assay/sso.py): people sign in with the company's identity provider (OpenID Connect)
    oidc_issuer: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_redirect_url: Optional[str] = None  # default: ASSAY_PUBLIC_URL/auth/callback
    oidc_admins: List[str] = field(default_factory=list)  # groups or emails that are admins
    oidc_managers: List[str] = field(default_factory=list)  # ... managers; everyone else reads
    oidc_role_claim: str = "groups"
    oidc_allowed_domains: List[str] = field(default_factory=list)
    oidc_tenant: str = "default"  # the tenant people who sign in belong to
    oidc_tenant_claim: Optional[str] = None  # or a claim that says which
    session_secret: Optional[str] = None
    session_hours: float = 12.0
    auth_mode: str = "auto"  # auto (on once any key exists) | required | off
    rate_limit_per_min: int = 1200  # per key, per instance; 0 = no limit
    cors_origins: List[str] = field(default_factory=list)
    webhook_secret: Optional[str] = None  # signs alert webhooks (X-Assay-Signature)

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

    # agent runs: evaluated when they end; quiet this long means abandoned (assay/lifecycle.py)
    abandon_minutes: float = 30.0
    evaluate_seconds: int = 60  # how often the sweep looks for abandoned and unevaluated runs (0 = off)
    backlog_minutes: float = 10.0  # ended runs waiting longer than this for evaluation open an alert
    judge_model: str = "claude-opus-5"  # the LLM judge (assay/judge.py), run on request
    judge_redact: bool = True  # personal data is replaced before a trace goes to the model API
    judge_provider: str = "anthropic"  # anthropic, openai, gemini, ollama or openai-compatible (assay_sdk.Judge)
    judge_concurrency: int = 4  # runs judged at once (assay_sdk.EvalRuntime); prices from ASSAY_PRICES
    judge_rate_limit: Optional[float] = None  # model calls a minute
    judge_max_time: Optional[float] = None  # seconds for one request's judging
    judge_budget_usd: Optional[float] = None  # dollars for one request's judging
    review_sample: int = 50  # production conversations read per review run (assay/review.py)
    review_budget_usd: Optional[float] = None  # dollars for one review run
    review_daily: bool = False  # read ASSAY_REVIEW_SAMPLE conversations a day, per scheduled events source

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
            source_password_command=e("ASSAY_SOURCE_PASSWORD_COMMAND"),
            downstream_url=e("ASSAY_DOWNSTREAM_URL"),
            downstream_hash_sql=e("ASSAY_DOWNSTREAM_HASH_SQL"),
            admin_key=e("ASSAY_ADMIN_KEY") or e("ASSAY_API_KEY"),
            oidc_issuer=e("ASSAY_OIDC_ISSUER"), oidc_client_id=e("ASSAY_OIDC_CLIENT_ID"),
            oidc_client_secret=e("ASSAY_OIDC_CLIENT_SECRET"), oidc_redirect_url=e("ASSAY_OIDC_REDIRECT_URL"),
            oidc_admins=_list(e("ASSAY_OIDC_ADMINS")), oidc_managers=_list(e("ASSAY_OIDC_MANAGERS")),
            oidc_role_claim=e("ASSAY_OIDC_ROLE_CLAIM", "groups"),
            oidc_allowed_domains=_list(e("ASSAY_OIDC_ALLOWED_DOMAINS")), oidc_tenant=e("ASSAY_OIDC_TENANT", "default"),
            oidc_tenant_claim=e("ASSAY_OIDC_TENANT_CLAIM"), session_secret=e("ASSAY_SESSION_SECRET"),
            session_hours=float(e("ASSAY_SESSION_HOURS", "12")),
            auth_mode=e("ASSAY_AUTH", "auto"),
            rate_limit_per_min=int(e("ASSAY_RATE_LIMIT_PER_MIN", "1200")),
            cors_origins=_list(e("ASSAY_CORS_ORIGINS")),
            webhook_secret=e("ASSAY_WEBHOOK_SECRET"),
            webhook_url=e("ASSAY_WEBHOOK_URL"),
            webhook_format=e("ASSAY_WEBHOOK_FORMAT", "slack"),
            public_url=e("ASSAY_PUBLIC_URL"),
            alert_min_n=int(e("ASSAY_ALERT_MIN_N", "30")),
            alert_after_runs=int(e("ASSAY_ALERT_AFTER_RUNS", "2")),
            abandon_minutes=float(e("ASSAY_ABANDON_MINUTES", "30")),
            evaluate_seconds=int(e("ASSAY_EVALUATE_SECONDS", "60")),
            backlog_minutes=float(e("ASSAY_BACKLOG_MINUTES", "10")),
            judge_model=e("ASSAY_JUDGE_MODEL", "claude-opus-5"),
            judge_redact=e("ASSAY_JUDGE_REDACT", "true").lower() not in ("0", "false", "no"),
            judge_provider=e("ASSAY_JUDGE_PROVIDER", "anthropic"),
            judge_concurrency=int(e("ASSAY_JUDGE_CONCURRENCY", "4")),
            judge_rate_limit=_num(e("ASSAY_JUDGE_RATE_LIMIT")),
            judge_max_time=_num(e("ASSAY_JUDGE_MAX_TIME")),
            judge_budget_usd=_num(e("ASSAY_JUDGE_BUDGET_USD")),
            review_sample=int(e("ASSAY_REVIEW_SAMPLE", "50")),
            review_budget_usd=_num(e("ASSAY_REVIEW_BUDGET_USD")),
            review_daily=e("ASSAY_REVIEW_DAILY", "false").lower() in ("1", "true", "yes"),
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
        return webhook_notifier(self.webhook_url, self.webhook_format, self.public_url, self.webhook_secret)
