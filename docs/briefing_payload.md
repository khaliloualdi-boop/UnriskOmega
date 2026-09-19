# Briefing payload — JSON contract (v0.1, draft)

The single JSON object the analytics engine hands to the LLM layer to write a
one-profile briefing. **Not definitive** — shapes may still change — but stable
enough to build against in parallel.

Two kinds of data live here:

- **Engine-produced** (deterministic, from `clients.json` + `reference.json`):
  `profile`, `performance`, `allocation`, `mandate`, `peers`, `projection`.
  These numbers are computed and tested; the LLM must **never** recompute or
  invent them — only describe them.
- **Externally-sourced** (`market_context`): produced by the market-data /
  news-scraping team, merged in before the LLM runs. This is the block to build
  against.

```
briefing = {
  "schema_version": "briefing/0.1",
  "generated_at":   "<ISO-8601 UTC, when the engine ran>",
  "analysis_date":  "<history-end date; the analysis 'as of' — NOT the run date>",
  "temporal":       { generated_at, analysis_date, positions_as_of, history_end,
                      fund_composition_date, note },   # engine — explicit dates

  "profile":        { … identity + mandate … },        # engine (assembler)
  "performance":    { basis, series, metrics{…} },      # engine (analytics.performance)
  "allocation":     { allocation{…}, diversification, coverage }, # engine (analytics.allocation)
  "top_holdings":   { holdings[…], as_of, weight_basis, coverage }, # engine (analytics.holdings)
  "accounts_by_currency": { by_currency[…], totals },   # engine (analytics.holdings) — cash vs crypto
  "lookthrough":    { dimensions{industry,region,currency}, coverage, limits }, # engine (analytics.lookthrough)
  "mandate":        { "saa_drift": …, "guardrails": … },# engine (analytics.mandate)
  "peers":          { … percentile vs cohort + market … }, # engine (analytics.peers)
  "projection":     { "bootstrap": {…}, "parametric": {…} }, # engine (analytics.simulation)

  "market_context": { … EXTERNAL — market-data team … },

  "data_quality":   { stage_status, unavailable[], coverage, conventions, notes }, # engine, honesty block
  "disclaimer":     "Illustrative; not investment advice."
}
```

Build it with `python build_briefing.py CASE-002 --output briefing.json` (accepts a
client ref or a portfolio id). `analytics.payload.build_briefing(store, ref)` is the
programmatic entry point.

---

## Engine blocks (reference — implemented)

Values are JSON-safe; a metric that could not be computed is `null` (never `0`,
never `"N/A"`). Fractions are 0–1 unless a `unit` says otherwise.

- **`profile`** — `client_ref`, `portfolio_id`, `portfolio_number`,
  `reporting_currency`, `portfolio_currency`, `aum`,
  `risk_profile{ id, name, max_vola, equity_quote_cap }`,
  `strategy{ id, name, vol_min, vol_max }`, `investment_service`.
- **`performance.metrics`** — each is `{ value, unit, label, … }`:
  `total_return, annualized_return, annualized_volatility, sharpe_ratio,
  sortino_ratio, downside_deviation, max_drawdown, calmar_ratio,
  value_at_risk_monthly_95, expected_shortfall_monthly_95, best_month,
  worst_month, positive_month_ratio, rolling_return_12m`. Plus `series`
  (start/end date, observations).
- **`allocation.allocation`** — `asset_class`, `currency_group`, `country_group`,
  `industry`, each `{ weights: {category: fraction}, classified_share }`.
  Plus `diversification{ hhi, effective_holdings, holdings, largest_holding }`
  and `coverage{ positions_vs_aum, cash_share, crypto_value_chf, crypto_share,
  has_crypto_account, has_short_position, … }`.
- **`mandate.saa_drift`** — per dimension, rows of
  `{ category, actual, target, min, max, drift, status }` where status ∈
  `within | below_min | above_max | no_limits | not_in_saa`; plus `breaches[]`.
- **`mandate.guardrails.checks`** — `volatility_vs_profile_max`,
  `equity_quote_vs_cap`, `volatility_vs_strategy_band`, each with
  `{ actual, limit|min/max, status }`; plus `breaches[]`.
- **`peers`** — `target`, `peer_group{ group_by, group_value, fallback_from,
  n_peers, small_sample }`, `comparison{ metric: { target, percentile_in_group,
  direction, group{min,median,max} } }`, and `market{ metric: distribution }`.
- **`projection`** —
  ```
  { "method": "bootstrap|parametric", "horizon_months": 60, "n_paths": 10000,
    "seed": 42, "assumptions": { "risk_free_annual": 0.0, "source": "…" },
    "fan_chart": [ { "month": 1, "p5":…, "p25":…, "p50":…, "p75":…, "p95":… }, … ],
    "terminal": { "p5":…, "p50":…, "p95":…, "mean":… },
    "probabilities": { "loss": …, "drawdown_gt_20pct": …, "reach_target": … } }
  ```

---

## News-module blocks (Priority 1, implemented)

- **`top_holdings`** — positions ranked by value; per holding: `security_id`, `isin`,
  `name`, `issuer_name` (⚠ reuses the instrument name — no distinct issuer field in
  source), `instrument_type` (from `Securities.SecurityTypeName`), `value`, `currency`,
  `weight_of_total`, `asset_class` / `sector` / `region` (SAA buckets), `classified`.
  Block-level: `as_of`, `weight_basis` (sum of security and account positions),
  `count` / `shown` / `omitted`.
- **`accounts_by_currency`** — accounts aggregated by currency code, each with
  `category` (`cash` / `crypto` / `unknown`), `value`, `weight_of_total`; plus `totals`
  = `cash_ex_crypto`, `crypto`, `cash_share_ex_crypto`, `crypto_share`. **No IBANs or
  account names.** (`allocation.coverage.cash_share` *includes* crypto — use these
  totals when you need them split.)
- **`performance`** now also carries `metrics.last_month_return` (with date),
  `metrics.ytd_return` (with `reference_date` + `partial_year`), and a **`basis`** block
  documenting that returns are value-based (NAV), monthly, and **not flow-adjusted**.
- **`lookthrough`** — funds decomposed into underlying `industry` / `region` / `currency`
  exposure (each `[{category, weight}]` on the securities book), with `coverage`
  (`fund_value_share`, `funds_covered`, `funds_without_composition`) and `limits`. Turns a
  fund-heavy portfolio's otherwise-`Unclassified` sector map into a real breakdown. **No
  underlying company names, no composition date** (source has neither); `region` is
  lower-confidence than industry/currency.
- **`temporal`** — explicit `generated_at`, `analysis_date` (= history end), 
  `positions_as_of`, `history_end`, `fund_composition_date` (null — not in source).
  Unknown dates stay null, never replaced by the run date.
- **`data_quality`** — `stage_status`, `unavailable[]` (block + reason), `coverage`,
  `conventions`, and standing `notes` (flow caveat, `Unclassified` vs `Not classified`
  /`Others`, snapshot-only positions, issuer-name caveat, shifted dates).

### Availability recap (requested field → path → status)

| Requested | Path in payload | Status |
|---|---|---|
| Ranked positions (id, isin, name, value, ccy, weight, class/sector/region) | `top_holdings.holdings[]` | ✓ |
| Instrument type | `top_holdings.holdings[].instrument_type` | ✓ |
| Issuer / public company name | `top_holdings.holdings[].issuer_name` | ⚠ instrument name reused; resolve downstream |
| Accounts by currency, cash vs crypto | `accounts_by_currency.by_currency[]` + `.totals` | ✓ |
| `cash_share` excludes crypto? | `accounts_by_currency.totals.cash_share_ex_crypto` | ✓ (allocation `cash_share` includes it) |
| Fund/ETF underlying sector/region/currency | `lookthrough.dimensions.{industry,region,currency}` | ✓ (region lower-confidence) |
| Fund top underlying **companies** | — | ✗ not in source |
| Last-month & YTD performance | `performance.metrics.last_month_return` / `.ytd_return` | ✓ |
| Return method / flow treatment | `performance.basis` | ✓ documented (NAV, not flow-adjusted) |
| Max drawdown + peak/trough dates | `performance.metrics.max_drawdown` | ✓ |
| Explicit dates (generated/analysis/positions/history) | `temporal` | ✓ |
| Fund composition date | `temporal.fund_composition_date` | ✗ null (not in source) |
| Data quality / coverage / conventions | `data_quality` | ✓ |

---

## `market_context` — the block your team fills

Externally sourced, time-sensitive, merged in just before the LLM runs. The
engine leaves it as `{"status": "to_be_provided_by_market_data_team"}` when no
news result is supplied. Pass the news module's JSON through
`build_briefing(..., news_result=result)` or `build_briefing.py --news result.json`
to populate this block.

**The one thing that makes this useful: tag every item with the same vocabulary
the engine already uses, so the LLM can join news to *this* portfolio's
exposures.** Those controlled vocabularies are:

| Tag field | Allowed values = categories that appear in the engine output |
|---|---|
| `asset_class` | SAA asset classes: `Shares`, `Bonds`, `Liquidity`, `Real estate`, `Specialties andCommodities` |
| `region` | SAA country groups: `North America`, `Switzerland`, `Rest of Europe`, `Japan`, `Asia/Pacific (ex Japan)`, `Great Britain`, `Others`, `Not classified` |
| `industry` | SAA industries: `Financials`, `Information Technology`, `Health Care`, `Industrials`, `Consumer Staples`, `Consumer Discretionary`, `Energy`, `Materials`, `Utilities`, `Telecommunication Services`, `Real Estate` |
| `currency` | SAA currency groups: `Swiss francs`, `Euro`, `US-Dollar`, `Andere` |
| `isin` / `security_id` | a specific held instrument (join to `Securities[].Isin` / `.Id`) |

Proposed shape:

```jsonc
"market_context": {
  "as_of": "2026-09-19T08:00:00Z",
  "sources": ["reuters", "bloomberg", "…"],      // provenance list

  "house_view": {                                 // optional, one paragraph + stances
    "summary": "…",
    "stances": [ { "asset_class": "Shares", "region": "North America",
                   "stance": "overweight|neutral|underweight", "rationale": "…" } ]
  },

  "macro": [                                      // broad indicators
    { "indicator": "SNB policy rate", "value": 0.0125, "as_of": "2026-09-18",
      "source": "SNB", "url": "…" }
  ],

  "asset_class_outlook": [                         // one per relevant bucket
    { "asset_class": "Bonds", "region": "Rest of Europe",
      "view": "…", "horizon": "3-6m", "source": "…", "published_at": "…" }
  ],

  "news": [                                        // the scraped feed
    { "headline": "…", "summary": "≤2 sentences", "url": "…",
      "source": "…", "published_at": "2026-09-17T…",
      "sentiment": "positive|neutral|negative",     // optional
      "relevance": {                                // ← join keys, use the vocab above
        "isins": ["CH0012032048"], "security_ids": [12345],
        "asset_classes": ["Shares"], "regions": ["Switzerland"],
        "industries": ["Health Care"], "currencies": ["Swiss francs"]
      }
    }
  ],

  "security_notes": [                              // instrument-specific context
    { "isin": "CH0012032048", "security_id": 12345,
      "note": "…", "source": "…", "published_at": "…" }
  ]
}
```

### Notes for the market-data team

- **Relevance tags are what matter.** A news item with no `relevance` still fits
  the schema but the LLM can't connect it to the portfolio. Prioritise tagging by
  `asset_classes` / `regions` / `industries` (broad, always joinable) and by
  `isins` when a story is instrument-specific.
- **Every item carries `source` + `url` + `published_at`.** The briefing must
  cite; undated/unsourced items will be dropped.
- **Keep `summary` short** (≤2 sentences) — this is fed to the LLM, so tokens
  count; put detail behind `url`.
- **You don't need the full holdings to start.** The join vocab (the table above)
  is fixed. To know *which* asset classes / regions / industries a given profile
  actually holds, read `allocation.allocation.*.weights` from the engine payload
  and scrape around those.
- **Never include** account identifiers or IBANs — those never appear in this
  payload and must not be added.
```
## Implemented news contract (v3)

The current integration uses `market_context.articles[]`. The extended
`news[]` proposal above is not the implemented wire contract.
Pass a collected news-3.0 (or compatible news-2.0) result via `news_result`;
query plans, fixture results and mismatched client/portfolio identities are
rejected. A missing result still leaves the existing placeholder intact.

Articles include evidence-linked `matches`, `exposures`, `supporting_terms`,
`relevance` tags and `event_terms`. `impact_status: not_assessed` means
relevance is not a measured economic impact or an attribution of historical
losses. Collection statistics report cache hits, new runs and budget blocking.
See [the news module guide](../news/README.md) for collection and caching.
