"""Run the cheap measures of tools/bench.py against their budgets.

The budgets are loosened by BURNER_BENCH_FACTOR, 5 unless set, since test
machines vary. tools/bench.py runs every measure, the slow ones included.
"""

from __future__ import annotations

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest

TOOL = Path(__file__).resolve().parents[1] / "tools" / "bench.py"
FACTOR = float(os.environ.get("BURNER_BENCH_FACTOR", "5"))
CHEAP = ["start", "calls", "stop", "rest", "reads", "combining"]


def load_bench():
    if "bench" not in sys.modules:
        spec = importlib.util.spec_from_file_location("bench", TOOL)
        module = importlib.util.module_from_spec(spec)
        sys.modules["bench"] = module  # dataclasses look their module up here.
        spec.loader.exec_module(module)
    return sys.modules["bench"]


@pytest.mark.parametrize("name", CHEAP)
def test_within_budget(name: str) -> None:
    bench = load_bench()
    results = bench.run([name], cpu_seconds=0)
    assert results
    over = [r for r in results if r.over(FACTOR)]
    assert not over, over


def test_bench_reports_over_budget() -> None:
    bench = load_bench()
    assert bench.Result("x", 2.0, "ms", 1.0).over(1.0)
    assert not bench.Result("x", 2.0, "ms", 1.0).over(5.0)
    assert not bench.Result("x", None, "ms", 1.0).over(1.0)


def test_cpu_time_has_subsecond_precision() -> None:
    bench = load_bench()
    before = bench._cpu_seconds(os.getpid())
    end = time.process_time() + 0.3
    while time.process_time() < end:
        pass
    assert 0.1 < bench._cpu_seconds(os.getpid()) - before < 5


def test_the_cpu_workload_outlives_the_sample() -> None:
    bench = load_bench()
    argv = bench.idle_child(900)
    assert float(argv[-1]) > 900 + 1


def test_a_missing_memory_reading_is_not_zero() -> None:
    bench = load_bench()
    gone = 2**22 + 12345  # No process has this pid.
    assert bench.rss_mb(gone) is None
    assert bench.peak_rss(None, gone) is None and bench.peak_rss(3.0, gone) == 3.0
    assert bench.peak_rss(None, os.getpid()) > 0
    with pytest.raises(RuntimeError, match="no memory sample"):
        bench.sampled(None, "hostile child")
    with pytest.raises(RuntimeError, match="exited before"):
        bench.measured_rss(gone, "session at rest")
