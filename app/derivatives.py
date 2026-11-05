"""Derivative-product suggestion for no-history SKUs.

Fuzzy-matches each no-history SKU against the pool of scorable (classified)
SKUs in the same run and returns top candidates likely to be its parent.

Uses stdlib difflib so no extra dependency is pulled in. Precision over
recall: it is better to return no suggestion than a wrong one.
"""
from __future__ import annotations
import re
from difflib import SequenceMatcher
from typing import Any

import pandas as pd

_SIZE_RE = re.compile(r"\b\d+(?:[.,]\d+)?\s*(?:kg|g|l|ml|ltr|lts|gm|pcs|un|un\.|m|cm|mm)\b", re.I)
_TRIM_RE = re.compile(r"[^a-z0-9 ]+")


def _norm(s: str) -> str:
    """Normalise a description for similarity comparison."""
    if not isinstance(s, str):
        return ""
    s = s.lower()
    s = _SIZE_RE.sub("", s)   # strip size tokens like 4Kg, 500g, 1L
    s = _TRIM_RE.sub(" ", s)
    return " ".join(s.split())


def _norm_code(code: str) -> str:
    """Strip trailing digits / size suffix so Kitty4Kg and Kitty10Kg share a stem."""
    if not isinstance(code, str):
        return ""
    stem = re.sub(r"[-_]?\d+(?:[.,]\d+)?[a-zA-Z]*$", "", code.strip().lower())
    return stem or code.lower()


def suggest_parents(
    sku_level: pd.DataFrame,
    max_suggestions_per_child: int = 3,
    similarity_threshold: float = 0.72,
    max_orphans: int = 300,
    max_pool: int = 2000,
) -> list[dict[str, Any]]:
    """Return list of {child_sku, candidates: [{parent_sku, score, link_type_guess}, ...]}.

    Performance notes:
      - Caps orphans at `max_orphans` (prefers dormant over no-history, both have status).
      - Only matches against the top-N scorable SKUs by rank (`max_pool`).
      - Uses a cheap first-token prefilter: candidate skipped early if its first
        description token doesn't share a stem with the orphan's first token.
      - On a 9k-SKU dataset with the defaults, this runs in ~1-2 seconds instead
        of >60s for the naive all-vs-all.
    """
    pool = sku_level[sku_level["class_abc"].isin(["A", "B", "C"])].copy()
    if pool.empty:
        return []

    # Narrow the parent pool to the top scorable by rank (A + top of B generally)
    pool = pool.sort_values("rank_overall").head(max_pool)
    pool["__norm_desc"] = pool["sku_description"].fillna("").map(_norm)
    pool["__norm_code"] = pool["sku_code"].fillna("").map(_norm_code)
    pool["__first_tok"] = pool["__norm_desc"].map(lambda s: s.split()[0] if s else "")

    # Narrow orphans: prefer dormant (more likely recoverable) then no-history
    orphans_df = sku_level[sku_level["class_abc"] == "N"].copy()
    if orphans_df.empty:
        return []
    orphans_df["__priority"] = orphans_df["status_derived"].map({"dormant": 0, "new-sku": 1, "no-history": 2}).fillna(3)
    orphans_df = orphans_df.sort_values(["__priority", "sku_code"]).head(max_orphans)
    orphans_df["__norm_desc"] = orphans_df["sku_description"].fillna("").map(_norm)
    orphans_df["__norm_code"] = orphans_df["sku_code"].fillna("").map(_norm_code)
    orphans_df["__first_tok"] = orphans_df["__norm_desc"].map(lambda s: s.split()[0] if s else "")

    # Index pool by first token for cheap prefilter
    pool_by_tok: dict[str, list[dict]] = {}
    for _, cand in pool.iterrows():
        pool_by_tok.setdefault(cand["__first_tok"], []).append({
            "sku_code": cand["sku_code"],
            "description": cand["sku_description"],
            "cls": cand["class_abc"],
            "norm_code": cand["__norm_code"],
            "norm_desc": cand["__norm_desc"],
        })

    suggestions: list[dict[str, Any]] = []
    for _, orphan in orphans_df.iterrows():
        o_desc = orphan["__norm_desc"]
        o_code = orphan["__norm_code"]
        o_tok = orphan["__first_tok"]
        if not o_desc and not o_code:
            continue

        # Only consider candidates that share the orphan's first token
        candidates = pool_by_tok.get(o_tok, [])
        if not candidates:
            continue

        scored: list[tuple[float, dict]] = []
        for cand in candidates:
            code_sim = SequenceMatcher(None, o_code, cand["norm_code"]).ratio() if o_code and cand["norm_code"] else 0.0
            desc_sim = SequenceMatcher(None, o_desc, cand["norm_desc"]).ratio() if o_desc and cand["norm_desc"] else 0.0
            # First-token already matched, so apply the bonus directly
            score = 0.55 * code_sim + 0.35 * desc_sim + 0.05
            if score >= similarity_threshold:
                scored.append((score, {
                    "parent_sku": cand["sku_code"],
                    "parent_description": cand["description"],
                    "parent_class": cand["cls"],
                    "score": round(float(score), 3),
                    "link_type_guess": _guess_link_type(orphan["sku_description"], cand["description"]),
                }))

        scored.sort(key=lambda x: -x[0])
        top = [c for _, c in scored[:max_suggestions_per_child]]
        if top:
            suggestions.append({
                "child_sku": orphan["sku_code"],
                "child_description": orphan["sku_description"],
                "child_status": orphan["status_derived"],
                "candidates": top,
            })

    return suggestions


def _guess_link_type(child_desc: str, parent_desc: str) -> str:
    """Rough guess at whether a new SKU is derivative, complementary or cannibal.

    The user confirms or overrides via the UI. This is only a starting hint.
    """
    if not child_desc or not parent_desc:
        return "derivative"
    c = child_desc.lower()
    p = parent_desc.lower()

    # Size variant of same product -> cannibalistic (eats share from siblings)
    child_size = _SIZE_RE.search(c)
    parent_size = _SIZE_RE.search(p)
    if child_size and parent_size and _norm(c) == _norm(p):
        return "cannibalistic"

    # Flavour / variant words frequently signal cannibals
    if any(w in c for w in ("flavour", "flavor", "sabor", "variant")):
        return "cannibalistic"

    return "derivative"
