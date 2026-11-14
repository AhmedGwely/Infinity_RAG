"""Reciprocal-rank fusion of ranked candidate lists."""

from __future__ import annotations


def reciprocal_rank_fusion(rankings: list[list[str]], k: int = 60, weights: list[float] | None = None) -> list[tuple[str, float]]:
    """Fuse ranked id lists; ids appearing in several lists accumulate score."""
    weights = weights or [1.0] * len(rankings)
    if len(weights) != len(rankings):
        raise ValueError("weights must match rankings")
    scores: dict[str, float] = {}
    for ranking, weight in zip(rankings, weights):
        seen: set[str] = set()
        for position, item in enumerate(ranking, start=1):
            if item in seen:
                continue
            seen.add(item)
            scores[item] = scores.get(item, 0.0) + weight / (k + position)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))
