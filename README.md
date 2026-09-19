# UnRiskOmega integrated processing engine

This repository loads the client and reference exports, preserves missing-data
semantics, reconciles portfolio values, and produces deterministic analytics for
the downstream 60-second AI briefing. The AI layer receives calculated facts and
data-quality notes; it does not calculate portfolio metrics itself.

The engine currently provides:

- portfolio reconciliation and concentration findings;
- performance and risk metrics from NAV history;
- allocation, diversification, mandate drift, and guardrail checks;
- ranked holdings, account/currency exposure, and fund look-through;
- peer comparisons without double-counting consolidated portfolios;
- seeded bootstrap and parametric projections; and
- one strict JSON briefing payload with explicit unavailable states.

Market/news context and final AI narration remain external integrations. The
payload leaves a clearly marked `market_context` block for that team.

## Run in VS Code

Open this folder in VS Code and select its Python environment. The project needs
Python 3.11 or newer and has no runtime dependencies outside the standard
library.

```bash
uv sync --dev
uv run pytest -q
uv run ruff check .
uv run ty check analytics processing build_briefing.py concentration.py reconciliation.py run_analysis.py main.py analytics_playground.py
```

Run the original analysis pipeline:

```bash
uv run python run_analysis.py --analysis-date 2026-09-19
```

This writes `outputs/analysis_results.json`. The supplied dataset contains 47
clients and 57 portfolio views. Eight portfolios have incomplete reconciliation,
and CASE-023 preserves its CHF 500 discrepancy instead of hiding it.

Build the complete LLM-ready briefing for a client reference or portfolio ID:

```bash
uv run python build_briefing.py CASE-002 --paths 10000 --output outputs/briefing.json
uv run python build_briefing.py 52253 --paths 10000 --output outputs/briefing.json
```

The output directory is created automatically. JSON writing is strict: NaN and
Infinity are rejected instead of being emitted as non-standard JSON. Use a
smaller `--paths` value during development for a faster run.

## Programmatic entry points

```python
from analytics.payload import build_briefing
from loader import load_store_from_files

store = load_store_from_files("clients.json", "reference.json")
payload = build_briefing(store, "CASE-002", n_paths=10_000, seed=42)
```

Load the store once and reuse it for many clients. `build_briefing` accepts a
client reference or an integer portfolio ID. Client selection excludes a
consolidated view when a normal portfolio is available, and peer cohorts exclude
consolidated views by default to avoid double-counting.

The lower-level integration remains available through:

- `processing.loader.load_store` for already parsed JSON;
- `processing.dossier.build_dossier` for one typed client/portfolio dossier;
- `pipeline.analyze_portfolio` for reconciliation and concentration detail; and
- `pipeline.build_briefing_payload` for the typed concentration handoff contract.

## Missing-data rules

- Missing, null, invalid, and explicitly empty collections remain distinct.
- Unknown numeric values stay `null`; they never become zero or `"N/A"`.
- NaN and Infinity are rejected during loading and final serialization.
- Missing security or account collections block calculations that need a full
  portfolio denominator.
- Unresolved securities remain visible in an `Unclassified` bucket.
- Account identifiers and IBANs never enter the briefing payload.
- Crypto is recognized from explicit supported tickers. Unknown currency codes
  remain `unknown` rather than being mislabeled as crypto.
- Position weights use security and account amounts in portfolio currency. AUM
  in a different default/reporting currency is not mixed into that denominator.
- Short positions and unavailable source fields are surfaced as coverage limits.
- Consolidated views are not summed with their components.

`clients_cleaned.json` is a legacy artifact and is not used by the pipeline.
The raw `clients.json` remains the source so missing-data information is retained.

## Briefing contract

The full payload contract and market-data handoff are documented in
`docs/briefing_payload.md`. Every block has a `status`; failed prerequisites
produce an explanatory `reason`. `data_quality` collects stage status, coverage,
and standing interpretation notes for the narration layer.

The generated payload uses schema `briefing/0.1`. Projections are reproducible
for the same input, path count, horizon, and seed. They are scenario analysis,
not forecasts or investment advice.
