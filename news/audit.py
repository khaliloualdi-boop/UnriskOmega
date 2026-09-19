"""Coverage audit over every client/portfolio. --plan is offline, --live is billable."""
import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from loader import load_store_from_files
from processing.contracts import to_jsonable
from processing.dossier import build_all

from .apify import ApifyNewsProvider
from .cache import CachedNewsProvider
from .context import build_news_context
from .models import NewsProviderError
from .planning import plan_news
from .service import collect_news


def audit_store(store, *, provider=None, as_of=None, max_queries=4):
    now = as_of or datetime.now(UTC)
    rows, unique_queries, clients = [], set(), set()
    for dossier in build_all(store, analysis_date=now.date()):
        context = build_news_context(store, dossier)
        plan = plan_news(dossier, context=context, max_queries=max_queries)
        clients.add(dossier.client_ref)
        unique_queries.update(q.text for q in plan.queries)
        row = {"client_ref": dossier.client_ref, "portfolio_id": dossier.portfolio_id,
               "is_consolidated": dossier.is_consolidated, "plan": to_jsonable(plan)}
        if provider is not None:
            row["result"] = to_jsonable(collect_news(
                dossier, provider, context=context, as_of=now, max_queries=max_queries,
            ))
        rows.append(row)
    return {
        "mode": "live_audit" if provider else "query_plan_audit", "as_of": now.isoformat(),
        "summary": {
            "clients": len(clients), "portfolios": len(rows),
            "with_queries": sum(bool(r["plan"]["queries"]) for r in rows),
            "without_queries": sum(not r["plan"]["queries"] for r in rows),
            "planned_queries": sum(len(r["plan"]["queries"]) for r in rows),
            "unique_queries": len(unique_queries),
            "portfolios_with_articles": (sum(bool(r["result"]["articles"]) for r in rows)
                                         if provider else None),
            "collection_stats": dict(getattr(provider, "stats", {})),
        },
        "note": "Coverage is not article quality. Review evidence on live results; empty results are valid.",
        "portfolios": rows,
    }


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--plan", action="store_true")
    mode.add_argument("--live", action="store_true")
    parser.add_argument("--clients", type=Path, default=root / "clients.json")
    parser.add_argument("--reference", type=Path, default=root / "reference.json")
    parser.add_argument("--max-queries", type=int, choices=range(1, 13), default=4)
    parser.add_argument("--max-runs", type=int, choices=range(201), default=4,
                        help="GLOBAL run budget across all portfolios, not per client.")
    parser.add_argument("--cache", type=Path, default=root / ".cache/news.sqlite3")
    parser.add_argument("--output", type=Path, help="New JSON file; parent directory must exist.")
    args = parser.parse_args()
    provider = None
    try:
        if args.output and (args.output.exists() or not args.output.parent.is_dir()):
            raise ValueError("Choose a new output filename in an existing directory.")
        store = load_store_from_files(args.clients, args.reference)
        if args.live:
            provider = CachedNewsProvider(ApifyNewsProvider(), args.cache, max_runs=args.max_runs)
        report = audit_store(store, provider=provider, max_queries=args.max_queries)
        text = json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False)
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(text + "\n")
            print(json.dumps(report["summary"]))
            print(str(args.output.resolve()))
        else:
            print(text)
        # A completed audit may reveal unavailable portfolios; make that visible to automation.
        return 1 if args.live and any(r["result"]["status"] in {"partial", "unavailable"}
                                     for r in report["portfolios"]) else 0
    except (OSError, ValueError, NewsProviderError, sqlite3.Error) as exc:
        parser.exit(2, f"{exc}\n")
    finally:
        if provider is not None:
            provider.close()


if __name__ == "__main__":
    raise SystemExit(main())
