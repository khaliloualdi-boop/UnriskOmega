"""Real Apify news, or --plan to inspect client-specific queries without a call."""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from loader import load_store_from_files
from processing.contracts import to_jsonable
from processing.dossier import build_dossier
from .apify import ApifyNewsProvider
from .models import NewsProviderError
from .queries import build_queries
from .service import collect_news, render_news_context


def main():
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("client_ref")
    parser.add_argument("--portfolio-id", type=int)
    parser.add_argument("--clients", type=Path, default=root / "clients.json")
    parser.add_argument("--reference", type=Path, default=root / "reference.json")
    parser.add_argument("--no-reference", action="store_true")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--live", action="store_true", help="Real Apify calls (billable); APIFY_TOKEN required.")
    mode.add_argument("--plan", action="store_true", help="Actual planned queries only; no generated articles or API calls.")
    parser.add_argument("--max-queries", type=int, default=4, choices=range(1, 13), metavar="1-12")
    parser.add_argument("--aliases", type=Path, help='JSON object: {"SecurityId": "verified public entity name"}')
    parser.add_argument("--format", choices=("json", "text"), default="json")
    parser.add_argument("--output", type=Path, help="Write a NEW separate result file; never overwrite an existing file.")
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    try:
        if args.output and (args.output.exists() or not args.output.parent.is_dir()):
            raise ValueError("--output needs an existing parent directory and a filename that does not exist.")
        aliases = None
        if args.aliases:
            raw = json.loads(args.aliases.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or any(not isinstance(v, str) or not v.strip() for v in raw.values()):
                raise ValueError("Aliases must be a JSON object of SecurityId to public name.")
            aliases = {int(k): v for k, v in raw.items()}
        store = load_store_from_files(args.clients, None if args.no_reference else args.reference)
        dossier = build_dossier(store, args.client_ref, args.portfolio_id, analysis_date=now.date())
        if args.plan:
            output = {
                "client_ref": dossier.client_ref, "portfolio_id": dossier.portfolio_id,
                "mode": "query_plan", "plan": build_queries(dossier, aliases=aliases, max_queries=args.max_queries),
            }
            text = json.dumps(to_jsonable(output), ensure_ascii=False, indent=2, allow_nan=False)
            code = 0
        else:
            result = collect_news(dossier, ApifyNewsProvider(), as_of=now,
                                  aliases=aliases, max_queries=args.max_queries)
            text = (
                json.dumps(to_jsonable(result), ensure_ascii=False, indent=2, allow_nan=False)
                if args.format == "json" else render_news_context(result)
            )
            code = 1 if result.status == "unavailable" else 0
        if args.output:
            with args.output.open("x", encoding="utf-8") as stream:
                stream.write(text + "\n")
            print(str(args.output.resolve()))
        else:
            print(text)
        return code
    except (NewsProviderError, ValueError, OSError) as exc:
        parser.exit(2, f"{exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
