"""Ground-truth validation.

When the SKU master carries an existing ABC label (velocity_declared), we compute
how well our run agrees with it. For a client like SAS that actively maintains
these labels, this is a sanity check. For a client like PetMaxi where the field
is stale, this reveals the maintenance gap.

Returned shape is deliberately thin: KPIs, a crosstab, and a sample of
disagreements. The UI renders the panel only when at least one labelled SKU
is present.
"""
from __future__ import annotations
from typing import Any

import pandas as pd


def compute_validation(sku_level: pd.DataFrame) -> dict[str, Any] | None:
    if "velocity_declared" not in sku_level.columns:
        return None

    tagged = sku_level[sku_level["velocity_declared"].isin(["A", "B", "C"])].copy()
    if tagged.empty:
        return None

    tagged["agrees"] = (tagged["velocity_declared"] == tagged["class_abc"])

    scored = tagged[tagged["class_abc"].isin(["A", "B", "C"])]
    agree_scored = int(scored["agrees"].sum())

    cross = pd.crosstab(tagged["velocity_declared"], tagged["class_abc"])
    classes = ["A", "B", "C", "N"]
    cross_dict = {
        row: {col: int(cross.loc[row, col]) if row in cross.index and col in cross.columns else 0
              for col in classes}
        for row in ["A", "B", "C"]
    }

    # Disagreement samples by direction
    disagreements = tagged[~tagged["agrees"]].copy()
    disagreements["from_to"] = disagreements["velocity_declared"] + " → " + disagreements["class_abc"]
    sample = (
        disagreements
        .sort_values(["from_to", "rank_overall"])
        .head(25)
        [["sku_code", "sku_description", "velocity_declared", "class_abc", "rank_overall", "freq_tasks", "vol_qty"]]
        .to_dict(orient="records")
    )

    return {
        "total_labeled": int(len(tagged)),
        "total_labeled_scorable": int(len(scored)),
        "agreement_overall_pct": round(int(tagged["agrees"].sum()) / len(tagged) * 100, 1),
        "agreement_scorable_pct": round(agree_scored / max(len(scored), 1) * 100, 1),
        "crosstab": cross_dict,
        "fell_to_n": {
            row: int(cross.loc[row, "N"]) if row in cross.index and "N" in cross.columns else 0
            for row in ["A", "B", "C"]
        },
        "disagreement_sample": sample,
    }
