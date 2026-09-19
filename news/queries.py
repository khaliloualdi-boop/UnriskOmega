"""Queries must name a held instrument or its identifiable issuer."""
import math
import re
import unicodedata
from collections import defaultdict
from dataclasses import replace

from processing.contracts import ClientDossier, SourceRef

from .models import HoldingLink, NewsQuery, QueryPlan

CRYPTO_NAMES = {"BTC": "Bitcoin", "ETH": "Ethereum", "DOT": "Polkadot",
                "SOL": "Solana", "SHIB": "Shiba Inu"}
ISIN = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")
AMBIGUOUS_NAMES = {
    "cat", "meta", "apple", "shell", "target", "gap", "visa", "alphabet",
    "total", "block", "next", "arm", "orange",
    "polkadot", "solana", "shiba inu",
}
# These are not public entity names and may never be used alone as aliases.
GENERIC_NAMES = {
    "shares", "stocks", "equities", "bonds", "equity markets", "bond markets",
    "interest rates", "real estate", "liquidity", "commodities",
    "financial news", "market news", "central bank", "monetary policy",
}


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.casefold())
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.findall(r"[a-z0-9]+", text))


def clean_security_name(name: str) -> str:
    """Remove source wrappers, preserving the public company or full fund name."""
    name = re.sub(
        r"^(?:Namen[- ]Aktie|Inhaber[- ]Aktie|Aktien|Aktie|Registered shares?|"
        r"Genussschein|Participation certificate|Anteile|Units)\s+",
        "", name.strip(), flags=re.IGNORECASE,
    )
    name = re.sub(r"^\s*-[^-]+-\s*", "", name)
    name = re.sub(r"^\d+(?:[.,]\d+)?\s*%\s*", "", name)
    name = re.sub(r"\s+\d{4}-\d{2}\.\d{2}\.\d{2,4}.*$", "", name)
    name = re.sub(r"\s+(?:AG|SA|Inc\.?|Ltd\.?|PLC|Corp\.?)$", "", name, flags=re.IGNORECASE)
    return " ".join(name.replace('"', "").split())


def _positive(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value > 0


def build_queries(dossier: ClientDossier, *, aliases=None, max_queries=4) -> QueryPlan:
    """Rank actual named exposures; all unsearched positions remain inspectable.

    No sector/asset-class fallback and no invented ticker or fund look-through.
    A fund keeps its full product identity; its manager alone is not an alias.
    """
    if not isinstance(max_queries, int) or isinstance(max_queries, bool) or not 1 <= max_queries <= 12:
        raise ValueError("max_queries must be an integer between 1 and 12.")
    aliases = aliases or {}
    plan = QueryPlan()
    groups = defaultdict(list)
    for collection in (dossier.security_positions, dossier.account_positions):
        if collection.unavailable:
            plan.warnings.append(f"{collection.path}: presence={collection.presence.value}")
    for pos in dossier.security_positions:
        if pos.security_id is None or not _positive(pos.total_amount_in_portfolio_currency):
            plan.skipped.append({
                "security_id": pos.security_id, "source_path": pos.source.path,
                "reason": "missing_id_or_nonpositive_or_unknown_value",
            })
            continue
        groups[pos.security_id].append(pos)

    candidates = []
    for sid, rows in groups.items():
        ref = dossier.securities.get(sid, {})
        ref = ref if isinstance(ref, dict) else {}
        raw_name = ref.get("Name") or rows[0].security_name
        name = aliases.get(sid) or raw_name
        name = clean_security_name(name) if isinstance(name, str) else ""
        key = normalize(name)
        if len(key) < 3 or key in GENERIC_NAMES or not any(c.isalpha() for c in key):
            plan.skipped.append({"security_id": sid, "reason": "unresolved_public_entity_name"})
            continue
        security_type = ref.get("SecurityTypeName", "")
        is_fund = security_type == "Investment fund" or bool(
            re.match(r"^(Anteile|Units)\b", raw_name or "", flags=re.IGNORECASE)
        )
        is_bond = security_type == "Bonds, debt register claims" or bool(
            re.match(r"^\d+(?:[.,]\d+)?\s*%", raw_name or "")
        )
        # Exact ISIN is safe only where the supplied reference does not contradict it.
        isin = rows[0].isin or ref.get("Isin")
        valid_isin = isinstance(isin, str) and bool(ISIN.fullmatch(isin))
        if rows[0].isin and ref.get("Isin") and rows[0].isin != ref["Isin"]:
            valid_isin = False
            plan.warnings.append(f"SecurityId={sid}: position/reference ISIN mismatch")
        terms: tuple[str, ...] = (name, isin) if valid_isin and isinstance(isin, str) else (name,)
        # Never match a fund using only the asset manager name.
        if is_fund and len(key.split()) < 3:
            plan.skipped.append({"security_id": sid, "reason": "fund_name_not_specific_enough"})
            continue
        query = f'"{name}"'
        if valid_isin:
            query += f' OR "{isin}"'
        if len(query) > 240:
            plan.skipped.append({"security_id": sid, "reason": "entity_name_needs_verified_short_alias"})
            continue
        try:
            amount = math.fsum(p.total_amount_in_portfolio_currency for p in rows)
        except OverflowError:
            plan.skipped.append({"security_id": sid, "reason": "position_value_overflow"})
            continue
        paths = tuple(p.source.path or "" for p in rows)
        relation = "fund" if is_fund else "bond_issuer" if is_bond else "instrument_or_issuer"
        link = HoldingLink(sid, str(raw_name or name), amount,
                           dossier.portfolio.portfolio_currency, relation, paths)
        evidence = [
            replace(p.source, field="TotalAmountInPortfolioCurrency",
                    value=p.raw.get("TotalAmountInPortfolioCurrency", p.total_amount_in_portfolio_currency),
                    path=f"{p.source.path}.TotalAmountInPortfolioCurrency")
            for p in rows
        ]
        evidence.append(
            SourceRef(source="news_alias_configuration", security_id=sid, field="public_name",
                      path=f"aliases[{sid}]", value=aliases[sid])
            if sid in aliases else
            SourceRef(source="reference.json" if ref.get("Name") else rows[0].source.source,
                      security_id=sid, field="Name" if ref.get("Name") else "SecurityName",
                      path=f"Securities[Id={sid}].Name" if ref.get("Name") else f"{paths[0]}.SecurityName",
                      value=raw_name)
        )
        candidates.append((amount, NewsQuery(query, relation, terms, (link,), tuple(evidence))))

    for account in dossier.account_positions:
        currency = (account.currency or "").upper()
        if currency not in CRYPTO_NAMES:
            plan.skipped.append({"source_path": account.source.path, "reason": "no_direct_entity_for_account"})
            continue
        if not _positive(account.total_amount_in_portfolio_currency):
            plan.skipped.append({"source_path": account.source.path, "reason": "nonpositive_or_unknown_crypto_value"})
            continue
        name = CRYPTO_NAMES[currency]
        amount = account.total_amount_in_portfolio_currency
        link = HoldingLink(None, name, amount, dossier.portfolio.portfolio_currency,
                           "crypto", (account.source.path or "",))
        evidence = (
            replace(account.source, field="Currency", value=currency,
                    path=f"{account.source.path}.Currency"),
            replace(account.source, field="TotalAmountInPortfolioCurrency", value=amount,
                    path=f"{account.source.path}.TotalAmountInPortfolioCurrency"),
        )
        candidates.append((amount, NewsQuery(f'"{name}"', "crypto", (name,), (link,), evidence)))

    # One search per entity, retaining every bond/share position linked to it.
    merged = {}
    for amount, query in candidates:
        key = (query.kind, normalize(query.match_terms[0]))
        if key not in merged:
            merged[key] = [amount, query]
        else:
            prior_amount, prior = merged[key]
            # Different SecurityIds are separate positions; preserve their combined exposure.
            merged[key] = [math.fsum((amount, prior_amount)), replace(
                prior, match_terms=tuple(dict.fromkeys(prior.match_terms + query.match_terms)),
                holdings=prior.holdings + query.holdings, evidence=prior.evidence + query.evidence,
            )]
    ordered = sorted(merged.values(), key=lambda pair: (-pair[0], pair[1].text))
    largest = ordered[0][0] if ordered else 1.0
    ranked = [replace(query, priority=amount / largest) for amount, query in ordered]
    plan.queries = ranked[:max_queries]
    plan.deferred_queries = ranked[max_queries:]
    if dossier.is_consolidated:
        plan.warnings.append(f"PortfolioId={dossier.portfolio_id}: consolidated view")
    return plan
