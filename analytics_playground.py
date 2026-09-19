"""Inspect one client through the same path used by the batch runner."""
import json
from datetime import UTC, datetime
from pathlib import Path

from loader import load_store_from_files
from pipeline import analyze_portfolio
from processing.dossier import build_dossier


def main():
    root = Path(__file__).resolve().parent
    store = load_store_from_files(root / "clients.json", root / "reference.json")
    dossier = build_dossier(store, "CASE-016", 315, analysis_date=datetime.now(UTC).astimezone().date())
    print(json.dumps(analyze_portfolio(dossier), indent=2, ensure_ascii=False, allow_nan=False))


if __name__ == "__main__":
    main()
