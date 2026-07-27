"""Hypothesis config schema and loading.

A hypothesis is a YAML file describing which named dataset to run against
and how to decide, per historical trade, whether that trade "would still
have been taken" under the alternative rule. Two ways to express that
decision:

  Level 1 — plain filters (no Python needed), e.g.:
      filters:
        exclude_pair_contains: ["NZD"]
        min_confidence: 6

  Level 2 — a custom rule function, for anything a filter can't express:
      custom_rule: rules/conf_reweight_v1.py::apply

New experiments should almost always be a new one-page YAML file (Level 1)
or a new small rule file (Level 2) — never a change to engine.py.
"""

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

import pandas as pd
import yaml

_SUPPORTED_FILTER_KEYS = {
    "exclude_pair_contains",   # list[str] — drop rows whose pair contains any of these
    "include_pair_contains",   # list[str] — keep only rows whose pair contains any of these
    "exclude_pairs",           # list[str] — drop rows with these exact pair values
    "include_pairs",           # list[str] — keep only rows with these exact pair values
    "min_confidence",          # number — drop rows with confidence below this
    "max_confidence",          # number — drop rows with confidence above this
    "regime_in",               # list[str] — keep only rows with market_regime in this list
    "direction_in",            # list[str] — keep only rows with direction in this list
}


@dataclass
class Hypothesis:
    name: str
    description: str
    dataset: str
    filters: dict = field(default_factory=dict)
    custom_rule: Optional[str] = None
    source_path: Optional[Path] = None


def load_hypothesis(path: str) -> Hypothesis:
    p = Path(path)
    with open(p, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw.get("name"):
        raise ValueError(f"{path}: hypothesis must have a 'name'")
    if not raw.get("dataset"):
        raise ValueError(f"{path}: hypothesis must have a 'dataset'")

    filters = raw.get("filters") or {}
    unknown = set(filters) - _SUPPORTED_FILTER_KEYS
    if unknown:
        raise ValueError(
            f"{path}: unsupported filter key(s) {sorted(unknown)}. "
            f"Supported: {sorted(_SUPPORTED_FILTER_KEYS)}. "
            f"For anything else, use a custom_rule instead."
        )

    return Hypothesis(
        name=raw["name"],
        description=raw.get("description", ""),
        dataset=raw["dataset"],
        filters=filters,
        custom_rule=raw.get("custom_rule"),
        source_path=p,
    )


def _load_custom_rule(rule_spec: str, hypothesis_path: Path) -> Callable[[dict], bool]:
    """rule_spec is 'relative/path.py::function_name'."""
    file_part, _, func_name = rule_spec.partition("::")
    if not func_name:
        raise ValueError(
            f"custom_rule '{rule_spec}' must be in 'path.py::function_name' form"
        )
    rule_path = (hypothesis_path.parent / file_part).resolve()
    if not rule_path.exists():
        raise FileNotFoundError(f"custom_rule file not found: {rule_path}")

    spec = importlib.util.spec_from_file_location(rule_path.stem, rule_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    if not hasattr(module, func_name):
        raise AttributeError(f"{rule_path} has no function '{func_name}'")
    return getattr(module, func_name)


def build_predicate(hyp: Hypothesis) -> Callable[[pd.Series], bool]:
    """Combine filters (AND) and/or a custom rule into one row-predicate.

    The predicate answers: "would this historical trade still have been
    taken under the hypothesis?" It never modifies the trade's real logged
    outcome — only whether the row is included in the hypothesis subset.
    """
    filters = hyp.filters

    def filter_predicate(row: pd.Series) -> bool:
        pair = str(row.get("pair", ""))
        if "exclude_pair_contains" in filters:
            if any(sub in pair for sub in filters["exclude_pair_contains"]):
                return False
        if "include_pair_contains" in filters:
            if not any(sub in pair for sub in filters["include_pair_contains"]):
                return False
        if "exclude_pairs" in filters:
            if pair in filters["exclude_pairs"]:
                return False
        if "include_pairs" in filters:
            if pair not in filters["include_pairs"]:
                return False
        if "min_confidence" in filters:
            conf = row.get("confidence")
            if pd.isna(conf) or conf < filters["min_confidence"]:
                return False
        if "max_confidence" in filters:
            conf = row.get("confidence")
            if pd.isna(conf) or conf > filters["max_confidence"]:
                return False
        if "regime_in" in filters:
            if str(row.get("market_regime", "")) not in filters["regime_in"]:
                return False
        if "direction_in" in filters:
            if str(row.get("direction", "")) not in filters["direction_in"]:
                return False
        return True

    if hyp.custom_rule:
        custom_fn = _load_custom_rule(hyp.custom_rule, hyp.source_path)

        def combined_predicate(row: pd.Series) -> bool:
            if not filter_predicate(row):
                return False
            return bool(custom_fn(row.to_dict()))

        return combined_predicate

    return filter_predicate
