from __future__ import annotations

import importlib.util
from pathlib import Path

TOOL = Path(__file__).resolve().parents[1] / "tools" / "record.py"


def load():
    spec = importlib.util.spec_from_file_location("record_tool", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_scrub_env_takes_prefixes_from_the_caller() -> None:
    record = load()
    env = {"PATH": "/bin", "TMUX": "x", "COLORTERM": "y", "APP_TOKEN": "z", "APPLE": "a"}
    assert record.scrub_env(env, []) == {"PATH": "/bin", "APP_TOKEN": "z", "APPLE": "a"}
    assert record.scrub_env(env, ["APP_"]) == {"PATH": "/bin", "APPLE": "a"}
