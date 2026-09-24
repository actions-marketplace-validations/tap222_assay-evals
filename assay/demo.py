"""Synthetic demo tenant so the dashboard works without connecting a pipeline.

A document-intelligence pipeline serving a handful of customers (the
segments), with some everyday gaps (one stage's calls aren't priced, a few
placeholder stages report success without doing anything) and four staged
incidents so alerting has something to catch:

- 6 to 4 days ago: field-extraction failures spike, then recover (alert resolves)
- last 3 days: classification calls get slow (alert stays open)
- last 4 days: a new customer starts sending a large share of traffic (drift)
- last 5 days: Globex Logistics documents stop reaching the downstream system
- last 4 days: Globex field extraction escalates to a pricier fallback model,
  so its cost per document jumps

Cost comes from per-page model prices, review and rework minutes for about a
fifth of documents (more for long contracts and claims), and an example rate
card for people time and platform overhead.

All of it is generated. Nothing here is real pipeline data.
"""
from __future__ import annotations

import random
from datetime import datetime, timedelta

from sqlalchemy import delete, select

from sqlalchemy.engine import Engine

from assay import store
from assay.models import Window
from assay.runner import run_measures
from assay.sources.events import EventsSource

TENANT = "demo"
SOURCE = f"events:{TENANT}"
SEGMENTS = ["Northwind Bank", "Contoso Insurance", "Globex Logistics", "Initech Health", None]
TYPES = ["invoice", "bank_statement", "contract", "id_document", "insurance_claim", None]
STAGES = ["document_splitting", "text_extraction", "classification", "field_extraction"]
MODELS = {"document_splitting": "gemini-3-flash-preview", "text_extraction": "gemini-3-flash-preview",
          "classification": "claude-haiku-4-5", "field_extraction": "claude-sonnet-5"}
LATENCY_MS = {"document_splitting": 4000, "text_extraction": 6000, "classification": 1500,
              "field_extraction": 9000}
PIPELINE = ["file_prep", "pre_processing", "text_extraction", "classification", "field_extraction",
            "validation", "highlighting", "redaction"]
STUBS = {"highlighting", "redaction"}  # placeholder stages: report success, do nothing
FALLBACK = {"document_splitting": "gemini-3.5-flash-lite", "field_extraction": "claude-opus-5-5"}
PRICE_PER_PAGE = {"gemini-3-flash-preview": 0.0006, "gemini-3.5-flash-lite": 0.0003,
                  "claude-haiku-4-5": 0.0009, "claude-sonnet-5": 0.004, "claude-opus-5-5": 0.02}
PAGES = {"invoice": (1, 3), "bank_statement": (3, 12), "contract": (8, 40), "id_document": (1, 2),
         "insurance_claim": (4, 20), None: (1, 10)}
TOUCH = {"invoice": 0.10, "bank_statement": 0.20, "contract": 0.45, "id_document": 0.08,
         "insurance_claim": 0.35, None: 0.25}
EXAMPLE_RATES = {"review_per_hour": 36.0, "rework_per_hour": 36.0,
                 "platform_per_document": 0.004, "platform_per_page": 0.0008}

EXAMPLE_SLOS = [
    ("fallback_attribution", None, None, 0.95, "Example target: every call says which tier answered"),
    ("cost_coverage", None, None, 0.99, "Example target: spend is a total, not a floor"),
    ("handoff_loss", "segment", None, 0.05, "Example target: no segment loses more than 5%"),
    ("stage_failure_rate", None, None, 0.02, "Example target"),
    ("call_error_rate", None, None, 0.02, "Example target"),
    ("call_latency_p95", "stage", "field_extraction", 20000, "Example target"),
    ("time_to_complete_p90", "processing_mode", "realtime", 4 * 3600, "Example target: realtime p90 under 4 h"),
]


def seed(engine: Engine, days: int = 56, docs_per_day: int = 120, seed_value: int = 11,
         window_days: int = 3) -> dict:
    rng = random.Random(seed_value)
    now = datetime.utcnow().replace(microsecond=0)
    ago = lambda ts: (now - ts).total_seconds() / 86400  # age in days
    rollout = 14  # pretend tier attribution for splitting shipped two weeks ago

    with engine.begin() as conn:
        for t in (store.event_calls, store.event_documents, store.event_stage_runs, store.event_indexed,
                  store.event_reviews):
            conn.execute(delete(t).where(t.c.tenant == TENANT))
        run_ids = [r[0] for r in conn.execute(select(store.measure_runs.c.id)
                                              .where(store.measure_runs.c.source == SOURCE))]
        if run_ids:
            conn.execute(delete(store.measure_results).where(store.measure_results.c.run_id.in_(run_ids)))
            conn.execute(delete(store.measure_runs).where(store.measure_runs.c.id.in_(run_ids)))
        conn.execute(delete(store.alerts).where(store.alerts.c.source == SOURCE))
        conn.execute(delete(store.slos).where(store.slos.c.source == SOURCE))
        conn.execute(delete(store.cost_rates).where(store.cost_rates.c.source == SOURCE))
        conn.execute(store.cost_rates.insert(), [dict(source=SOURCE, key=k, value=v, updated_at=now)
                                                 for k, v in EXAMPLE_RATES.items()])
        conn.execute(store.slos.insert(), [dict(source=SOURCE, measure_id=m, dimension=d, slice_value=v,
                                                target=t, note=n, updated_at=now)
                                           for m, d, v, t, n in EXAMPLE_SLOS])

    calls, docs, runs, indexed, reviews = [], [], [], [], []
    for day in range(days):
        for k in range(docs_per_day):
            received = now - timedelta(days=days - day) + timedelta(minutes=rng.randint(0, 1439))
            age = ago(received)
            did = f"demo-{day:03d}-{k:03d}"
            segment = "Umbrella Legal" if age < 4 and rng.random() < 0.3 else rng.choice(SEGMENTS)
            itype = rng.choice(TYPES)
            mode = "batch" if rng.random() < 0.35 else "realtime"
            if mode == "realtime":
                minutes = rng.lognormvariate(3.75, 1.2)  # median ~43 min, long tail
                completed = received + timedelta(minutes=minutes) if rng.random() < 0.97 else None
            else:
                completed = received + timedelta(hours=rng.uniform(2, 30)) if rng.random() < 0.32 else None
            if completed and completed > now:
                completed = None
            lost_rate = 0.45 if (segment == "Globex Logistics" and age < 5) else 0.02
            fh = f"sha256:{rng.getrandbits(64):016x}"
            pages = rng.randint(*PAGES[itype])
            docs.append(dict(tenant=TENANT, document_id=did, received_at=received, completed_at=completed, page_count=pages,
                             status="completed" if completed else "processing", processing_mode=mode,
                             file_hash=fh, segment=segment, document_type=itype,
                             delivered_downstream=(rng.random() >= lost_rate) if completed else None))
            for s, stage in enumerate(PIPELINE):
                start = received + timedelta(seconds=30 * s)
                fail_p = 0.12 if (stage == "field_extraction" and 4 <= age < 6) else 0.004
                failed = stage not in STUBS and rng.random() < fail_p
                runs.append(dict(tenant=TENANT, run_id=f"{did}-{stage}", document_id=did, stage=stage,
                                 status="failed" if failed else "success", started_at=start,
                                 finished_at=start + timedelta(seconds=0.1 if stage in STUBS else 20),
                                 did_work=stage not in STUBS))
            for stage in STAGES:
                ts = received + timedelta(seconds=rng.randint(10, 600))
                attributed = rng.random() < (0.97 if (stage == "document_splitting" and ago(ts) < rollout) else 0.6)
                served = MODELS[stage]
                escalate = 0.6 if (stage == "field_extraction" and segment == "Globex Logistics"
                                   and ago(ts) < 4) else 0.05 if stage == "document_splitting" else 0.03 \
                    if stage == "field_extraction" else 0.0
                if rng.random() < escalate:
                    served = FALLBACK[stage]
                    attributed = True if stage == "field_extraction" else attributed
                billed_pages = min(pages, 2) if stage == "classification" else pages
                price = round(PRICE_PER_PAGE[served] * billed_pages * rng.lognormvariate(0, 0.2), 5)
                slow = 3.5 if (stage == "classification" and ago(ts) < 3) else 1.0
                calls.append(dict(
                    tenant=TENANT, call_id=f"{did}-{stage}", stage=stage, ts=ts, document_id=did,
                    model_declared=MODELS[stage], model_served=served,
                    resolving_layer=("primary" if served == MODELS[stage] else "fallback_1") if attributed else None,
                    gate_reason=("ok" if served == MODELS[stage] else "low_confidence") if attributed else None,
                    # text extraction isn't priced at all, flash-lite fallbacks never are, and 5% of
                    # other calls lose their price: the ledger estimates what it can and says so.
                    cost_usd=None if (stage == "text_extraction" or served == "gemini-3.5-flash-lite"
                                      or rng.random() < 0.05) else price,
                    code_revision=None if rng.random() < 0.03 else "a1b2c3d",
                    segment=segment, document_type=itype,
                    latency_ms=round(LATENCY_MS[stage] * slow * rng.lognormvariate(0, 0.35)),
                    status="error" if rng.random() < 0.008 else "success"))
            if rng.random() < TOUCH[itype]:
                rts = received + timedelta(minutes=rng.randint(15, 240))
                reviews.append(dict(tenant=TENANT, review_id=f"{did}-r", document_id=did, ts=rts, kind="review",
                                    minutes=round(rng.lognormvariate(1.1, 0.5) * (1 + pages / 10), 1),
                                    reviewer=f"reviewer-{rng.randint(1, 6)}", stage="field_extraction"))
                if rng.random() < 0.3:
                    reviews.append(dict(tenant=TENANT, review_id=f"{did}-w", document_id=did,
                                        ts=rts + timedelta(minutes=30), kind="rework",
                                        minutes=round(rng.lognormvariate(1.8, 0.5), 1),
                                        reviewer=f"reviewer-{rng.randint(1, 6)}", stage="field_extraction"))
            indexed.append(dict(tenant=TENANT, extraction_id=f"{did}-x", document_id=did, has_positions=rng.random() < 0.85,
                                segment=segment, document_type=itype))

    with engine.begin() as conn:
        conn.execute(store.event_documents.insert(), docs)
        conn.execute(store.event_stage_runs.insert(), runs)
        conn.execute(store.event_calls.insert(), calls)
        conn.execute(store.event_indexed.insert(), indexed)
        conn.execute(store.event_reviews.insert(), reviews)

    # Backfill one run per day over a rolling window, oldest first, so alerts
    # open and resolve in the order they would have live.
    source = EventsSource(engine, TENANT)
    run_ids = []
    for d in range(days - 2 * window_days, -1, -1):
        end = now - timedelta(days=d)
        run_ids.append(run_measures(engine, source, Window(end - timedelta(days=window_days), end), as_of=end))
    with engine.connect() as conn:
        a = store.alerts
        open_n = len(conn.execute(select(a.c.id).where((a.c.source == SOURCE) & (a.c.state == "open"))).all())
        resolved_n = len(conn.execute(select(a.c.id).where((a.c.source == SOURCE) & (a.c.state == "resolved"))).all())
    return {"documents": len(docs), "calls": len(calls), "stage_runs": len(runs), "reviews": len(reviews),
            "runs": len(run_ids),
            "alerts_open": open_n, "alerts_resolved": resolved_n}
