"""Scenario loading. Scenario files are JSON validated against `GameState`."""

from __future__ import annotations

from importlib.resources import files

from flattop.model import GameState


def load_scenario(name: str) -> GameState:
    text = files("flattop.scenarios").joinpath(f"{name}.json").read_text(encoding="utf-8")
    return GameState.model_validate_json(text)


def list_scenarios() -> list[str]:
    return sorted(p.name.removesuffix(".json") for p in files("flattop.scenarios").iterdir() if p.name.endswith(".json"))
