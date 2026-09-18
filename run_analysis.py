"""Run the integrated data-to-findings pipeline without an AI dependency."""
import argparse
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path

from loader import load_store_from_files
from pipeline import IMPLEMENTED_FINDING_RULES, analyze_portfolio
from processing.contracts import SCHEMA_VERSION, to_jsonable
from processing.dossier import build_all

ROOT = Path(__file__).resolve().parent


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=Path, default=ROOT / "clients.json")
    parser.add_argument("--reference", type=Path, default=ROOT / "reference.json")
    parser.add_argument("--no-reference", action="store_true")
    parser.add_argument("--analysis-date", type=date.fromisoformat, default=datetime.now(timezone.utc).astimezone().date())
    parser.add_argument("--output", type=Path, default=ROOT / "outputs" / "analysis_results.json")
    args = parser.parse_args(argv)
    reference_path = None if args.no_reference else args.reference
    if reference_path is not None and not reference_path.exists():
        parser.error(f"Reference file not found: {reference_path}. Use --no-reference to omit it.")
    store = load_store_from_files(args.clients, reference_path)
    reports = [
        analyze_portfolio(dossier)
        for dossier in build_all(store, analysis_date=args.analysis_date)
    ]
    output = {
        "schema_version": SCHEMA_VERSION,
        "analysis_date": args.analysis_date.isoformat(),
        "implemented_finding_rules": IMPLEMENTED_FINDING_RULES,
        "loader_diagnostics": to_jsonable(store.diagnostics),
        "portfolio_reports": reports,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, indent=2, ensure_ascii=False, allow_nan=False), encoding="utf-8")
    checks = Counter(r["checks"]["largest_position_concentration"]["status"] for r in reports)
    print("Clients processed:", len(store.clients))
    print("Portfolios processed:", len(reports))
    print("Largest-position checks:", dict(checks))
    print("Findings generated:", sum(len(r["findings"]) for r in reports))
    print("Loader errors:", len(store.errors()))
    print("Results saved to:", args.output)
    return 1 if store.errors() else 0


if __name__ == "__main__":
    raise SystemExit(main())
