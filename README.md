# UnRiskOmega integrated processing milestone

Mohamed's data layer now supplies Khalil's analytics through `ClientDossier`.
The runner loads the original client data and optional reference data, reconciles
portfolio values, calculates concentration, and produces shared `Finding`,
`ActionCandidate`, and `BriefingPayload` objects with evidence and data gaps.

This milestone implements **largest-security-position concentration findings**.
It is not the complete three-section briefing product: allocation, suitability,
performance, ranking, market/news analysis, and AI narration remain future work.
The output explicitly labels unimplemented checks rather than implying they passed.
Top-five concentration is calculated as a metric but does not yet emit a finding.

## Run in VS Code

Open this project folder. Python 3.14 and `uv` are the versions/tools specified
by the existing project; no new runtime dependencies were added.

```bash
uv run python run_analysis.py --analysis-date 2026-09-19
uv run pytest -q
```

If a project environment is already active, `python run_analysis.py` and
`python -m pytest -q` work as well. Explicit analysis dates make runs repeatable.
The CLI otherwise defaults to the machine's current local date.

Results are saved to `outputs/analysis_results.json`. The supplied data produces:

- 47 clients and 57 separate portfolio reports.
- 40 triggered, 8 non-triggered, and 9 unavailable largest-position checks.
- 40 findings and no loader errors on this dataset.
- 8 incomplete reconciliations and the preserved CHF 500 CASE-023 discrepancy.

The default `main.py` runs the same integrated pipeline. It no longer creates
an `"N/A"`-filled data copy. `clients_cleaned.json` is retained only as a legacy
file and is not used. `analytics_playground.py` inspects CASE-016 through the
same integrated functions; it does not maintain a separate calculation path.

## API handoff

The API team can call this without running the CLI or writing a file:

```python
import json
from datetime import date
from pathlib import Path

from processing.loader import load_store
from processing.dossier import build_dossier
from processing.contracts import to_jsonable
from pipeline import build_briefing_payload

clients = json.loads(Path("clients.json").read_text())
reference = json.loads(Path("reference.json").read_text())
store = load_store(clients, reference)
dossier = build_dossier(store, "CASE-016", 315, analysis_date=date(2026, 9, 19))
payload = build_briefing_payload(dossier)
result = to_jsonable(payload)
json.dumps(result, allow_nan=False)
```

`load_store` takes parsed JSON, not a filename. `loader.load_store_from_files`
is a thin file-reading helper that calls that same loader. Load once and reuse
the store. New client batches use the same entry point. This project does not
implement the API upload endpoint or an incremental persistence layer.

For detailed reconciliation results, call `pipeline.analyze_portfolio(dossier)`.
It returns a JSON-compatible report containing `briefing_payload`. Do not pass
raw dictionaries to the analytics functions anymore.

## Contract changes in schema 1.1

- `reconcile_portfolio(dossier)` replaces the raw-dictionary/issues-list interface.
- `analyze_concentration(dossier, reconciliation)` consumes normalized objects.
- `build_concentration_finding(dossier, concentration)` returns a shared `Finding`.
- `analyze_portfolio(dossier)` returns the detailed report.
- `build_briefing_payload(dossier)` returns the typed integration payload.
- `Finding` adds optional `analyzer` and `calculation`, and requires evidence.
- Actions are separate `ActionCandidate` objects linked by finding IDs, with an
  optional `rationale`. They are no longer nested inside a finding.
- Serialized `SourceList` values preserve `items`, `presence`, `path`, and
  `raw_type`. Missing/null/invalid lists must not collapse to bare empty lists.
- Evidence paths are client-keyed, such as
  `clients[CASE-016].Portfolios[0].SecurityPositions[0].TotalAmountInPortfolioCurrency`.
  Resolve the client by `ClientRef`; this is a source locator convention, not
  standard JSONPath. Paths and finding IDs remain stable when a client is loaded
  in a smaller batch. Source values retain their original type/spelling.

## Missing data and boundaries

- Invalid or missing numerical inputs never become zero. Numeric strings that
  convert to Infinity/NaN are rejected.
- A mixed valid/invalid list keeps usable rows for inspection but is marked
  invalid. Its relevant loader diagnostics travel with the dossier.
- Missing security or account collections block reconciliation. A missing key
  does not automatically mean a confirmed cash-only portfolio.
- Concentration needs valid amounts, positive AUM, unique security IDs, and a
  matched reconciliation. Short security positions need a separate policy and
  are explicitly unavailable in this milestone.
- An explicitly empty security list on an otherwise valid reconciled portfolio
  makes security concentration `not_applicable`, not a general all-clear.
- The 10% largest-position and 40% top-five thresholds are configurable screening
  rules, not verified client policy limits. Classification-based checks are not
  implemented here.
- AUM in the export's default currency is interpreted using the client's
  `ReportingCurrency`. Without a known matching `PortfolioCurrency`, the check
  is unavailable until FX/default-currency semantics are provided. This assumption
  is included in findings and should be confirmed for production exports.
- Historical NAV observations and portfolio snapshot dates are separate fields.
  History recency warnings refer to the latest history date, not the snapshot.
- Portfolios are analyzed separately, including consolidated views. Do not sum
  their AUM or findings into client-wide exposure without removing overlap.
- Dossier availability means inputs are available, not that an analyzer ran.
  Use the actual `checks` and `data_gaps` when generating text.
- `load_store` retains invalid-record diagnostics; the CLI writes inspectable
  results and returns a nonzero exit status if the store contains loader errors.
  API consumers should inspect these diagnostics too.

## Optional reference data and alternate input

```bash
uv run python run_analysis.py --no-reference --analysis-date 2026-09-19
uv run python run_analysis.py --clients new_clients.json --reference reference.json
```

Concentration works without reference data. Future dependent checks must honor
missing-reference availability. An explicitly requested missing file is an error;
it is not silently replaced with empty reference data.

## Validation

The integrated test suite includes Mohamed's 38 existing tests plus 32 regression
and integration cases: all-portfolio output, known numbers, non-triggered checks,
unavailable checks, malformed records, non-finite numbers, missing values, source
evidence, partial-batch stability, the 10% boundary, serialization, and the CLI.

The data files are unchanged. No AI calls, network fetches, or trade execution
are part of this pipeline.
