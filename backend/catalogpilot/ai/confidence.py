"""Confidence engine (06_AI_Design 7).

A model's own "confidence" is not used. The score combines measurable signals into a raw
value, then maps it through a calibrator fitted on a labeled benchmark, so that
"confidence 0.97" means "about 97% of proposals scoring like this were correct".
The auto-approve threshold tau per task is the smallest raw score at which measured
precision is at least 98%. Without a calibration file nothing is ever auto-approved.
"""

from __future__ import annotations

import bisect
import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

CALIBRATION_FILE = Path(__file__).with_name("calibration.json")
TARGET_PRECISION = 0.98
MIN_SUPPORT = 40

# Weights of the raw score (w2..w5 in the design; log-probabilities are not available from
# every provider, so that signal is dropped and the calibrator is fitted without it).
WEIGHTS = {"self_consistency": 0.15, "retrieval": 0.35, "validators": 0.25, "change": 0.25}


def raw_score(task: str, signals: dict[str, float]) -> float:
    """All inputs are in [0, 1]; so is the result."""
    change_penalty = signals.get("edit_ratio", 0.0) / 0.6  # 0.6 is the validator's hard limit
    if task == "category":
        retrieval = 0.5 * signals.get("retrieval_score", 0.0) + 0.5 * signals.get("retrieval_margin", 0.0)
    elif task == "attributes":
        retrieval = 1.0 if signals.get("grounded", 0.0) else 0.0
    else:
        retrieval = 1.0 - min(1.0, change_penalty)
    score = (
        WEIGHTS["self_consistency"] * signals.get("self_consistency", 1.0)
        + WEIGHTS["retrieval"] * retrieval
        + WEIGHTS["validators"] * signals.get("validator_pass_ratio", 0.0)
        + WEIGHTS["change"] * (1.0 - min(1.0, change_penalty))
    )
    score -= 0.3 * signals.get("case_ambiguity", 0.0)
    return max(0.0, min(1.0, score))


# ---------------------------------------------------------------- isotonic calibration

def fit_isotonic(pairs: list[tuple[float, bool]]) -> list[tuple[float, float]]:
    """Pool-adjacent-violators. Returns steps (raw_upper_bound, precision), non-decreasing."""
    pairs = sorted(pairs)
    blocks: list[list[float]] = []  # [sum_correct, count, max_raw]
    for raw, correct in pairs:
        blocks.append([1.0 if correct else 0.0, 1.0, raw])
        while len(blocks) > 1 and blocks[-2][0] / blocks[-2][1] > blocks[-1][0] / blocks[-1][1]:
            s, c, m = blocks.pop()
            blocks[-1][0] += s
            blocks[-1][1] += c
            blocks[-1][2] = m
    return [(b[2], b[0] / b[1]) for b in blocks]


def threshold_for_precision(pairs: list[tuple[float, bool]], target: float = TARGET_PRECISION,
                            min_support: int = MIN_SUPPORT) -> float | None:
    """Smallest raw threshold t such that precision over {raw >= t} is >= target, with enough
    examples above it to trust the estimate. None means: never auto-approve this task."""
    ordered = sorted(pairs, reverse=True)
    best = None
    correct = 0
    for i, (raw, ok) in enumerate(ordered, start=1):
        correct += 1 if ok else 0
        last_of_tie = i == len(ordered) or ordered[i][0] != raw
        if last_of_tie and i >= min_support and correct / i >= target:
            best = raw
    return best


def expected_calibration_error(pairs: list[tuple[float, bool]], steps: list[tuple[float, float]], bins: int = 10) -> float:
    if not pairs:
        return 0.0
    buckets: list[list[float]] = [[0.0, 0.0, 0.0] for _ in range(bins)]
    for raw, ok in pairs:
        conf = apply_steps(steps, raw)
        b = min(bins - 1, int(conf * bins))
        buckets[b][0] += conf
        buckets[b][1] += 1.0 if ok else 0.0
        buckets[b][2] += 1
    return sum(abs(b[0] / b[2] - b[1] / b[2]) * b[2] for b in buckets if b[2]) / len(pairs)


def apply_steps(steps: list[tuple[float, float]], raw: float) -> float:
    if not steps:
        return 0.0
    i = bisect.bisect_left([s[0] for s in steps], raw)
    return steps[min(i, len(steps) - 1)][1]


# ---------------------------------------------------------------- runtime

@dataclass(frozen=True)
class Calibration:
    steps: dict[str, list[tuple[float, float]]]
    tau: dict[str, float | None]
    meta: dict[str, Any]


@lru_cache
def load() -> Calibration:
    if not CALIBRATION_FILE.exists():
        return Calibration({}, {}, {"fitted": False})
    data = json.loads(CALIBRATION_FILE.read_text(encoding="utf-8"))
    return Calibration(
        {task: [tuple(s) for s in steps] for task, steps in data["steps"].items()},
        data["tau"], data.get("meta", {}),
    )


def score(task: str, model: str, signals: dict[str, float]) -> tuple[float, bool]:
    """Returns (calibrated confidence, eligible_for_auto_approval)."""
    raw = raw_score(task, signals)
    cal = load()
    # A calibration is only valid for the provider it was measured on.
    if task not in cal.steps or cal.meta.get("model") != model:
        return round(min(raw, 0.5), 4), False  # uncalibrated: capped, never auto-approved
    confidence = apply_steps(cal.steps[task], raw)
    tau = cal.tau.get(task)
    return round(confidence, 4), tau is not None and raw >= tau
