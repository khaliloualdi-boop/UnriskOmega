"""Deterministic, evidence-linked planning from holdings and analytics exposures."""
import math
from dataclasses import replace

from processing.contracts import SourceRef

from .context import fraction
from .models import ExposureLink, NewsQuery
from .queries import CRYPTO_NAMES, build_queries
from .topics import (
    ASSETS,
    CURRENCY_GROUPS,
    EVENTS,
    MONETARY,
    POLICY_EVENTS,
    REGIONS,
    SECTORS,
    UNKNOWN,
)


def _topic(dimension, category, weight, basis, path, subjects, events):
    subject_query = " OR ".join(f'"{s}"' for s in subjects[:2])
    event_query = '"interest rates" OR inflation OR "monetary policy"' if dimension in {"currency", "currency_pair"} else (
        "earnings OR inflation OR regulation OR outlook OR volatility"
    )
    specificity = {"industry": 0.9, "currency_pair": 0.85, "currency": 0.65,
                   "region": 0.6, "asset_class": 0.35}[dimension]
    return NewsQuery(
        text=f"({subject_query}) ({event_query})", kind="market_exposure",
        match_terms=tuple(subjects), holdings=(),
        evidence=(SourceRef(source="analytics", path=path, field="exposure",
                            value={"category": category, "weight": weight, "basis": basis}),),
        priority=(weight if weight is not None else 0.25) * specificity,
        exposures=(ExposureLink(dimension, category, weight, basis, path),),
        required_groups=(tuple(events),),
        priority_basis="reported_weight_x_specificity" if weight is not None else "known_exposure_unknown_weight",
    )


def plan_news(dossier, *, context=None, aliases=None, max_queries=4):
    """Direct-only API remains supported; analytics enables exposure-based searches.

    Budget allocation: reserve up to half for the strongest direct holdings and
    half for distinct exposure dimensions, then fill remaining slots by priority.
    This is a search budget heuristic, not a claim of financial impact.
    """
    plan = build_queries(dossier, aliases=aliases, max_queries=max_queries)
    if context is None:
        return plan
    if (context.client_ref, context.portfolio_id) != (dossier.client_ref, dossier.portfolio_id):
        raise ValueError("News context does not belong to the selected client/portfolio.")
    blocks = context.blocks
    plan.warnings.extend(context.warnings)
    direct = plan.queries + plan.deferred_queries
    holdings_block = blocks.get("top_holdings", {})
    holdings = holdings_block.get("holdings", []) if holdings_block.get("status") == "calculated" else []
    weights = {}
    for row in holdings:
        if isinstance(row, dict) and isinstance(row.get("security_id"), int):
            weights.setdefault(row["security_id"], []).append(fraction(row.get("weight_of_total")))
    revised = []
    for query in direct:
        if query.kind == "crypto":
            account_block = blocks.get("accounts_by_currency", {})
            for row in account_block.get("by_currency", []):
                if (row.get("category") == "crypto"
                        and CRYPTO_NAMES.get(row.get("currency")) == query.match_terms[0]):
                    weight = fraction(row.get("weight_of_total"))
                    if weight is not None:
                        query = replace(query, priority=weight, priority_basis="reported_weight",
                                        exposures=(ExposureLink(
                                            "crypto", row["currency"], weight,
                                            account_block.get("weight_basis", "unknown"),
                                            f"accounts_by_currency.by_currency[{row['currency']}]",
                                        ),))
        pieces = [w for sid in query.security_ids for w in weights.get(sid, [None])]
        if pieces and all(w is not None for w in pieces):
            weight = fraction(sum(pieces))
            if weight is not None:
                links = tuple(ExposureLink(
                    "security", str(sid), sum(weights[sid]), holdings_block.get("weight_basis", "unknown"),
                    f"top_holdings.holdings[security_id={sid}]",
                ) for sid in dict.fromkeys(query.security_ids))
                query = replace(query, priority=weight * (0.6 if query.kind == "fund" else 1),
                                exposures=links, priority_basis="reported_weight_x_specificity")
        revised.append(query)
    direct = sorted(revised, key=lambda q: (-q.priority, q.text))

    market = []
    allocation = blocks.get("allocation", {})
    allocations = allocation.get("allocation", {}) if allocation.get("status") == "calculated" else {}
    look = blocks.get("lookthrough", {})
    # Do not turn net short portfolios into long exposure topics.
    shorts = allocation.get("coverage", {}).get("has_short_position", False)
    if shorts:
        plan.warnings.append("Market exposure planning skipped: short-position attribution is unsupported.")

    def add_dimension(dimension, weights_map, basis, path, vocabulary, threshold=0.10):
        for category, raw_weight in weights_map.items():
            weight = fraction(raw_weight)
            if category in UNKNOWN or category not in vocabulary:
                plan.skipped.append({"source_path": path, "category": category,
                                     "reason": "unmapped_or_unclassified_exposure"})
                continue
            if weight is None or weight < threshold:
                plan.skipped.append({"source_path": path, "category": category,
                                     "reason": "unknown_or_below_market_topic_threshold"})
                continue
            market.append(_topic(dimension, category, weight, basis, f"{path}[{category}]",
                                 vocabulary[category], EVENTS))

    if not shorts:
        # Prefer look-through for a dimension only when it is usable. Reject invalid region labels.
        for dimension, alloc_name, vocabulary in (
            ("industry", "industry", SECTORS), ("region", "country_group", REGIONS),
        ):
            rows = look.get("dimensions", {}).get(dimension, []) if look.get("status") == "calculated" else []
            for row in rows:
                if isinstance(row, dict) and (row.get("category") not in vocabulary
                                               or fraction(row.get("weight")) is None):
                    plan.skipped.append({"source_path": f"lookthrough.dimensions.{dimension}",
                                         "category": row.get("category"),
                                         "reason": "unmapped_or_invalid_lookthrough_category"})
            usable = {r.get("category"): r.get("weight") for r in rows
                      if isinstance(r, dict) and r.get("category") in vocabulary
                      and fraction(r.get("weight")) is not None}
            if usable:
                add_dimension(dimension, usable, look.get("weight_basis", "securities book"),
                              f"lookthrough.dimensions.{dimension}", vocabulary)
                plan.warnings.append(f"{dimension}: look-through is category-level; composition date is unknown.")
            else:
                block = allocations.get(alloc_name, {})
                add_dimension(dimension, block.get("weights", {}), block.get("basis", "unknown"),
                              f"allocation.allocation.{alloc_name}.weights", vocabulary)
        asset = allocations.get("asset_class", {})
        add_dimension("asset_class", asset.get("weights", {}), asset.get("basis", "unknown"),
                      "allocation.allocation.asset_class.weights", ASSETS, threshold=0.25)

    # Currency topics stay independent of sector/region: no invented cross-exposures.
    currency_topics = {}
    accts = blocks.get("accounts_by_currency", {})
    if accts.get("status") == "calculated":
        for row in accts.get("by_currency", []):
            if not isinstance(row, dict) or row.get("category") != "cash":
                if isinstance(row, dict) and row.get("category") == "unknown":
                    plan.skipped.append({"category": row.get("currency"),
                                         "reason": "unknown_account_currency_category"})
                continue
            value = row.get("value")
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or not math.isfinite(value) or not value > 0):
                continue
            code = row.get("currency")
            if code not in MONETARY:
                plan.skipped.append({"category": code, "reason": "unmapped_cash_currency"})
                continue
            weight = fraction(row.get("weight_of_total"))
            # Unknown denominator does not erase an explicitly known cash holding.
            if weight is not None and weight < 0.05:
                continue
            currency_topics[code] = _topic(
                "currency", code, weight, accts.get("weight_basis", "unknown"),
                f"accounts_by_currency.by_currency[{code}]", MONETARY[code], POLICY_EVENTS,
            )
            base = context.portfolio_currency
            if base in MONETARY and code != base:
                market.append(_topic(
                    "currency_pair", f"{code}/{base}", weight, accts.get("weight_basis", "unknown"),
                    f"accounts_by_currency.by_currency[{code}]",
                    (f"{code}/{base}", f"{code}{base}"), POLICY_EVENTS + ("rises", "falls", "rallies", "plunges"),
                ))
    if not shorts:
        block = allocations.get("currency_group", {})
        weights_map = block.get("weights", {})
        basis = block.get("basis", "unknown")
        path = "allocation.allocation.currency_group.weights"
        currency_rows = look.get("dimensions", {}).get("currency", []) if look.get("status") == "calculated" else []
        underlying = {r.get("category"): r.get("weight") for r in currency_rows
                      if isinstance(r, dict) and r.get("category") in CURRENCY_GROUPS
                      and fraction(r.get("weight")) is not None}
        if underlying:
            weights_map, basis, path = underlying, look.get("weight_basis", "securities book"), "lookthrough.dimensions.currency"
        for category, raw_weight in weights_map.items():
            code, weight = CURRENCY_GROUPS.get(category), fraction(raw_weight)
            if code is None or weight is None or weight < 0.15:
                continue
            query = _topic("currency", code, weight, basis,
                           f"{path}[{category}]",
                           MONETARY[code], POLICY_EVENTS)
            prior = currency_topics.get(code)
            # Aggregate allocation already includes accounts: never sum the two weights.
            if prior:
                query = replace(query, exposures=query.exposures + prior.exposures,
                                evidence=query.evidence + prior.evidence)
            currency_topics[code] = query
    market.extend(currency_topics.values())
    market.sort(key=lambda q: (-q.priority, q.text))

    direct_slots = (1 if any(q.kind == "fund" for q in direct)
                    and all(q.kind in {"fund", "crypto"} for q in direct) else max(1, max_queries // 2))
    selected = direct[:direct_slots] if market else direct[:max_queries]
    used_dimensions = set()
    for query in market:
        dimension = query.exposures[0].dimension
        if len(selected) < max_queries and dimension not in used_dimensions:
            selected.append(query)
            used_dimensions.add(dimension)
    for query in sorted(direct + market, key=lambda q: (-q.priority, q.text)):
        if len(selected) >= max_queries:
            break
        if query not in selected:
            selected.append(query)
    plan.queries = selected
    plan.deferred_queries = [q for q in direct + market if q not in selected]
    if market:
        plan.skipped = [s for s in plan.skipped if s.get("reason") != "no_direct_entity_for_account"]
    return plan
