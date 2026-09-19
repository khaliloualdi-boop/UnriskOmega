# News search — why it returns nothing, and two ways to fix it

## The diagnosis (from `diag.json`, CASE-002)

The news module returns **0 articles**, and the reason is now certain:

- `rejected_counts: {}` → **no** article was filtered out by relevance or date. Nothing ever reached those filters.
- The queries sent to Apify are the **entire raw fund name + ISIN**, e.g.
  `"Credit Suisse Index Fd (CH) Umbrella - CSIF (CH) Equity Switzerland Large Cap Blue" OR "CH0214404714"`.
- No news article contains that 90-character string or an ISIN, so Yahoo returns **0 results per query**.
- `failed_queries: 2` → two queries even timed out on these empty searches (this is also why it's slow / "partial").

So the bug is in **query generation** (`news/queries.py`), not in the relevance filter that was tuned earlier. Both the **search query** and the **match terms** are the full fund name and must become short and searchable.

---

## Option A — Focused rewrite of `news/queries.py`

Derive a compact, searchable query from each holding: **issuer / brand + underlying exposure**, and set the `match_terms` to those same short tokens.

Examples of the transformation:

| Full fund name (today's query) | New query (searchable) |
|---|---|
| Credit Suisse Index Fd (CH) … Equity Switzerland Large Cap | `"Credit Suisse" Swiss equities` |
| UBS (Lux) Fund Solutions … MSCI World SRI UCITS ETF | `"UBS" "MSCI World" ETF` |
| SSGA SPDR … Bloomberg Global Aggregate Bond UCITS ETF | `SPDR "Global Aggregate" bond` |
| AB SICAV I … Short Duration High Yield Portfolio | `AllianceBernstein "high yield"` |

How: extract the leading brand tokens before the first structural word (Fund/SICAV/ETF/…), optionally add the asset-class/region from the reference classification we already compute, and use those as both the query text and the match terms.

**Pros**
- One file changed; keeps the current module intact.
- Fastest path to *actual results* — directly kills the root cause.
- Also speeds things up (short queries return quickly instead of timing out).

**Cons**
- Brand extraction from messy fund strings is heuristic; a few odd names may need a small alias map.
- Still one search per holding — no topic breadth or caching beyond what exists.

**Effort:** small–moderate, ~1 file. **Risk:** low.

---

## Option B — Adopt the v3 query/topic module

Port `news/topics.py` + `news/planning.py` (and their hooks) from the `news-v3-integration` folder. v3 was built for exactly this problem: it plans searches by **exposure and financial topic/event** (sector, region, asset class, known events) rather than the literal instrument name, and adds a **SQLite run-cache** that avoids repeat Apify calls.

**Pros**
- Purpose-built: broader, more resilient relevance (topics/events, not just brand strings).
- The cache is a real speed win on repeat runs.
- Comes with its own tests and an integration guide.

**Cons**
- Bigger change: 12 news files + porting hooks into `ai_engine.py`, `analytics/payload.py`, `news_integration.py`, `build_briefing.py`, plus tests.
- Built on an **older commit (`0bb809f`)**; our repo has diverged (chatbot merge, ai_engine split, lookthrough/holdings/payload) → careful manual porting required, higher conflict risk.
- Slower to land safely; harder to review in one sitting.

**Effort:** large, ~20 files. **Risk:** medium (integration conflicts).

---

## Recommendation

**Do Option A now** — it fixes the actual root cause quickly and gets real articles + faster runs with low risk. Keep **Option B** as a follow-up if you want topic-level breadth and the caching, done carefully as its own task rather than under time pressure.

| | A — queries.py rewrite | B — v3 topics/planning |
|---|---|---|
| Fixes "no articles" | ✅ yes (root cause) | ✅ yes |
| Speed | ✅ faster (short queries) | ✅ faster (cache) |
| Files touched | ~1 | ~20 |
| Risk | low | medium (diverged repo) |
| Time to ship | short | longer |
| Best as | immediate fix | planned follow-up |
