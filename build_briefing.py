"""Dump the briefing payload JSON for one profile.

Usage:
    python build_briefing.py CASE-002
    python build_briefing.py 52253 --paths 2000 --output briefing.json
"""
import argparse
import json
from pathlib import Path

from analytics.payload import build_briefing
from loader import load_store_from_files

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("profile", help="client ref (CASE-002) or portfolio id")
    ap.add_argument("--clients", type=Path, default=ROOT / "clients.json")
    ap.add_argument("--reference", type=Path, default=ROOT / "reference.json")
    def positive_int(value: str) -> int:
        parsed = int(value)
        if parsed < 1:
            raise argparse.ArgumentTypeError("must be at least 1")
        return parsed

    ap.add_argument("--paths", type=positive_int, default=10000, help="Monte Carlo paths")
    ap.add_argument("--news", type=Path, help="news-module JSON to attach as market_context")
    ap.add_argument("--output", type=Path, help="write JSON here instead of stdout")
    args = ap.parse_args(argv)

    store = load_store_from_files(args.clients, args.reference)
    profile: object = args.profile
    try:
        profile = int(args.profile)      # accept a numeric portfolio id
    except ValueError:
        pass

    news_result = None
    if args.news:
        try:
            news_result = json.loads(args.news.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            ap.error(f"Could not read --news JSON: {exc}")
        if not isinstance(news_result, dict):
            ap.error("--news must contain one JSON object")

    payload = build_briefing(store, profile, n_paths=args.paths, news_result=news_result)
    text = json.dumps(payload, indent=2, ensure_ascii=False, allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print("wrote", args.output)
    else:
        print(text)
    return 0 if payload.get("status") != "unavailable" else 1


if __name__ == "__main__":
    raise SystemExit(main())
