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
) -> list[dict[str, Any]]:
    """Return list of {child_sku, candidates: [{parent_sku, score, link_type_guess}, ...]}.

    Only no-history and dormant SKUs are candidates for linking. Parents are
    drawn from the active (scorable) pool.
    """
    pool = sku_level[sku_level["class_abc"].isin(["A", "B", "C"])].copy()
    if pool.empty:
        return []

    pool["__norm_desc"] = pool["sku_description"].fillna("").map(_norm)
    pool["__norm_code"] = pool["sku_code"].fillna("").map(_norm_code)

    orphans = sku_level[sku_level["class_abc"] == "N"].copy()
    orphans["__norm_desc"] = orphans["sku_description"].fillna("").map(_norm)
    orphans["__norm_code"] = orphans["sku_code"].fillna("").map(_norm_code)

    suggestions: list[dict[str, Any]] = []
    for _, orphan in orphans.iterrows():
        o_desc = orphan["__norm_desc"]
        o_code = orphan["__norm_code"]
        if not o_desc and not o_code:
            continue

        scored: list[tuple[float, dict]] = []
        for _, cand in pool.iterrows():
            # Code-stem match carries most weight
            code_sim = SequenceMatcher(None, o_code, cand["__norm_code"]).ratio() if o_code and cand["__norm_code"] else 0.0
            desc_sim = SequenceMatcher(None, o_desc, cand["__norm_desc"]).ratio() if o_desc and cand["__norm_desc"] else 0.0
            # Boost when first few tokens overlap
            first_tok_bonus = 0.0
            o_tokens = o_desc.split()[:3]
            c_tokens = cand["__norm_desc"].split()[:3]
            if o_tokens and c_tokens and o_tokens[0] == c_tokens[0]:
                first_tok_bonus = 0.05
                if len(o_tokens) > 1 and len(c_tokens) > 1 and o_tokens[1] == c_tokens[1]:
                    first_tok_bonus = 0.10

            score = 0.55 * code_sim + 0.35 * desc_sim + first_tok_bonus
            if score >= similarity_threshold:
                scored.append((score, {
                    "parent_sku": cand["sku_code"],
                    "parent_description": cand["sku_description"],
                    "parent_class": cand["class_abc"],
                    "score": round(float(score), 3),
                    "link_type_guess": _guess_link_type(orphan["sku_description"], cand["sku_description"]),
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
