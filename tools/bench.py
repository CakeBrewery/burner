"""Measure what sessions cost and compare each measure with its budget.

Usage:
    python tools/bench.py [--only NAME,...] [--cpu-seconds N]

It prints one row per measure and exits with status 1 when one is over its
budget. BURNER_BENCH_FACTOR multiplies every budget, for slow machines. The
sessions it starts run in a temporary run dir, and it stops them and removes
the dir when done. It needs only the standard library.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from burner import Host  # noqa: E402
from burner.host import LOG_LIMIT_BYTES  # noqa: E402
from burner.screen import Screen  # noqa: E402

PY = sys.executable
SLEEP = [PY, "-c", "import time; print('ready', flush=True); time.sleep(600)"]
FAST = (
    "import os, time\nend = time.monotonic() + 2\nblock = b'x' * 65535 + b'\\n'\n"
    "while time.monotonic() < end:\n    os.write(1, block)\n"
)
# Fills every cell with a letter and 31 combining marks, again and again.
COMBINING_FLOOD = (
    "import os, sys\ncols, rows = int(sys.argv[1]), int(sys.argv[2])\n"
    "row = ('a' + '\\u0301' * 31) * cols\n"
    "while True:\n    for y in range(rows):\n"
    "        os.write(1, ('\\x1b[%d;1H' % (y + 1) + row).encode())\n"
)
QUERY_FLOOD = (
    "import os, tty\ntty.setraw(0)\nprint('ready', end='\\r\\n', flush=True)\n"
    "while True:\n    os.write(1, b'\\x1b[6n' * 1024)\n"
)


@dataclass
class Result:
    name: str
    value: float | None  # None when the machine cannot measure it.
    unit: str
    budget: float

    def over(self, factor: float) -> bool:
        return self.value is not None and self.value > self.budget * factor


class Bench:
    """A temporary run dir and the sessions started in it."""

    def __init__(self) -> None:
        self.run_dir = Path(tempfile.mkdtemp(prefix="burner-bench-", dir="/tmp"))
        self.host = Host(self.run_dir, timeout=10)
        self.count = 0

    def start(self, argv: list[str], terminal: bool = False, **kwargs) -> str:
        self.count += 1
        name = f"s{self.count}"
        self.host.start(name, argv, cwd="/", terminal=terminal, **kwargs)
        log = self.run_dir / f"{name}.log"
        if terminal:
            _wait(lambda: "ready" in self.host.screen(name))
        else:
            _wait(lambda: log.exists() and "ready" in log.read_text())
        return name

    def host_pid(self, name: str) -> int:
        return json.loads((self.run_dir / f"{name}.json").read_text())["pid"]

    def close(self) -> None:
        """Stop every session. Remove the run dir only when all of them stopped."""
        left = []
        for lock in self.run_dir.glob("*.lock"):
            name = lock.name[: -len(".lock")]
            try:
                if not self.host.gone(name):  # Running, or not known to be gone.
                    self.host.stop(name, grace=1.0)
            except Exception as exc:
                left.append(f"{name}: {exc}")
        if left:
            print(f"sessions still in {self.run_dir}: " + "; ".join(left), file=sys.stderr)
        else:
            shutil.rmtree(self.run_dir, ignore_errors=True)


def _wait(check: Callable[[], object], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not check():
        if time.monotonic() > deadline:
            raise TimeoutError("the session did not get ready")
        time.sleep(0.01)


def _ms(seconds: float) -> float:
    return round(seconds * 1000, 2)


def _timed(call: Callable[[], object], times: int) -> float:
    """The median seconds one call takes."""
    samples = []
    for _ in range(times):
        started = time.perf_counter()
        call()
        samples.append(time.perf_counter() - started)
    return statistics.median(samples)


def rss_mb(pid: int) -> float | None:
    """The resident memory of a process in MB, or None when the process is gone."""
    out = subprocess.run(["ps", "-o", "rss=", "-p", str(pid)], capture_output=True, text=True)
    text = out.stdout.strip()
    return round(int(text) / 1024, 1) if text else None


def measured_rss(pid: int, what: str) -> float:
    """The resident memory of a process that must still run."""
    rss = rss_mb(pid)
    if rss is None:
        raise RuntimeError(f"{what}: the host exited before its memory was measured")
    return rss


def peak_rss(peak: float | None, pid: int) -> float | None:
    """The larger of peak and the current reading. A missing reading changes nothing."""
    rss = rss_mb(pid)
    if rss is None:
        return peak
    return rss if peak is None else max(peak, rss)


def sampled(peak: float | None, what: str) -> float:
    if peak is None:
        raise RuntimeError(f"{what}: no memory sample was taken")
    return peak


def bench_start(b: Bench) -> list[Result]:
    samples = []
    for _ in range(10):
        b.count += 1
        name = f"s{b.count}"
        started = time.perf_counter()
        b.host.start(name, SLEEP, cwd="/")
        samples.append(time.perf_counter() - started)
        b.host.stop(name)
    return [Result("start p50", _ms(statistics.median(samples)), "ms", 100)]


def bench_calls(b: Bench) -> list[Result]:
    tui = b.start([PY, "-c", "import time; print('ready', flush=True); time.sleep(600)"],
                  terminal=True)
    for _ in range(14):
        b.start(SLEEP)
    return [
        Result("alive", _ms(_timed(lambda: b.host.alive(tui), 50)), "ms", 5),
        Result("screen", _ms(_timed(lambda: b.host.screen(tui), 50)), "ms", 5),
        Result("write", _ms(_timed(lambda: b.host.write(tui, b"x"), 50)), "ms", 5),
        Result("sessions(15)", _ms(_timed(b.host.sessions, 20)), "ms", 5),
    ]


def bench_stop(b: Bench) -> list[Result]:
    samples = []
    for _ in range(5):
        name = b.start(SLEEP)
        started = time.perf_counter()
        b.host.stop(name)
        samples.append(time.perf_counter() - started)
    return [Result("stop, polite child", _ms(statistics.median(samples)), "ms", 30)]


def bench_rest(b: Bench) -> list[Result]:
    log = b.start(SLEEP)
    tui = b.start(SLEEP, terminal=True)
    time.sleep(1)
    return [
        Result("host RSS at rest, log",
               measured_rss(b.host_pid(log), "log session at rest"), "MB", 16),
        Result("host RSS at rest, terminal",
               measured_rss(b.host_pid(tui), "terminal session at rest"), "MB", 16),
    ]


def bench_fast_writer(b: Bench) -> list[Result]:
    b.count += 1
    name = f"s{b.count}"
    b.host.start(name, [PY, "-c", FAST], cwd="/")
    pid = b.host_pid(name)
    rss, disk = None, 0.0
    while b.host.alive(name):
        rss = peak_rss(rss, pid)
        disk = max(disk, sum(_size(p) for p in b.run_dir.glob(f"{name}.log*")))
        time.sleep(0.05)
    return [
        Result("host RSS, fast writer", sampled(rss, "fast writer"), "MB", 24),
        Result("log disk per session", round(disk / 1024 / 1024, 1), "MB",
               2 * LOG_LIMIT_BYTES / 1024 / 1024),
    ]


def _size(path: Path) -> int:
    try:
        return path.stat().st_size
    except FileNotFoundError:  # The host moved it in between.
        return 0


def bench_hostile(b: Bench) -> list[Result]:
    results = []
    for cols, rows, budget in ((80, 24, 32), (250, 400, 64)):
        name = b.start([PY, "-c", QUERY_FLOOD], terminal=True, cols=cols, rows=rows)
        pid, peak = b.host_pid(name), None
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            peak = peak_rss(peak, pid)
            time.sleep(0.25)
        b.host.stop(name)
        what = f"host RSS, hostile child {cols}x{rows}"
        results.append(Result(what, sampled(peak, what), "MB", budget))
    return results


def bench_combining(b: Bench) -> list[Result]:
    """Host memory while a child keeps 250x400 full of combining marks and a client reads it."""
    cols, rows = 250, 400
    b.count += 1
    name = f"s{b.count}"
    b.host.start(name, [PY, "-c", COMBINING_FLOOD, str(cols), str(rows)], cwd="/",
                 terminal=True, cols=cols, rows=rows)
    pid, peak = b.host_pid(name), None
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        b.host.screen(name)
        peak = peak_rss(peak, pid)
    b.host.stop(name)
    what = f"host RSS, combining flood {cols}x{rows}"
    return [Result(what, sampled(peak, what), "MB", 64)]


def _fill(prefix: bytes, unit: bytes) -> bytes:
    return (prefix + unit * 4096)[:4096]


HOSTILE_READS = {
    "rep": _fill(b"x", b"\x1b[65535b"),
    "wide rep": _fill("\u4e00".encode(), b"\x1b[65535b"),
    "insert rep": _fill(b"\x1b[4hx", b"\x1b[65535b"),
    "erase display": _fill(b"", b"\x1b[2J"),
    "reset": _fill(b"", b"\x1bc"),
    "alignment": _fill(b"", b"\x1b#8"),
    "combining": _fill(b"a", "\u0301".encode()),
}


def bench_reads(b: Bench) -> list[Result]:
    results = []
    for cols, rows, budget in ((80, 24, 100), (250, 70, 100), (250, 400, 1000)):
        worst = 0.0
        for data in HOSTILE_READS.values():
            screen = Screen(cols, rows)
            started = time.perf_counter()
            screen.feed(data)
            worst = max(worst, time.perf_counter() - started)
        results.append(Result(f"worst 4 KiB PTY read {cols}x{rows}", _ms(worst), "ms", budget))
    return results


def _wakeups(pid: int, seconds: float) -> float | None:
    """Wakeups per second of an idle process, or None where this cannot be read."""
    status = Path(f"/proc/{pid}/status")
    if status.exists():
        def switches() -> int:
            text = status.read_text()
            return int(re.search(r"voluntary_ctxt_switches:\s*(\d+)", text).group(1))

        before = switches()
        time.sleep(seconds)
        return round((switches() - before) / seconds, 2)
    if sys.platform == "darwin" and shutil.which("top"):
        # top prints context switch counts, which only grow. It skips the pid in its first sample.
        out = subprocess.run(
            ["top", "-l", "3", "-s", str(int(seconds)), "-pid", str(pid), "-stats", "pid,csw"],
            capture_output=True, text=True,
        ).stdout
        counts = [int(line.split()[1].rstrip("+")) for line in out.splitlines()
                  if line.split()[:1] == [str(pid)]]
        if len(counts) >= 2:
            return round((counts[-1] - counts[-2]) / int(seconds), 2)
    return None


def bench_wakeups(b: Bench) -> list[Result]:
    name = b.start(SLEEP)
    time.sleep(1)
    return [Result("wakeups at rest", _wakeups(b.host_pid(name), 5), "per s", 0.5)]


def _cpu_seconds(pid: int) -> float:
    """CPU seconds a process used, with subsecond precision."""
    stat = Path(f"/proc/{pid}/stat")
    if stat.exists():
        # utime and stime, in clock ticks, follow the name in parentheses.
        fields = stat.read_bytes().rsplit(b")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")
    # macOS ps prints hundredths of a second, as in 0:00.03 or 1-02:03:04.05.
    out = subprocess.run(["ps", "-o", "time=", "-p", str(pid)], capture_output=True, text=True)
    total = 0.0
    for part in out.stdout.strip().replace("-", ":").split(":"):
        total = total * 60 + float(part)
    return total


def idle_child(seconds: float) -> list[str]:
    """A child that prints ready and then sleeps past a sample of the given length."""
    return [PY, "-c", "import sys, time; print('ready', flush=True); "
            "time.sleep(float(sys.argv[1]))", str(seconds + 60)]


def bench_cpu(b: Bench, seconds: float) -> list[Result]:
    name = b.start(idle_child(seconds))
    pid = b.host_pid(name)
    time.sleep(1)
    before = _cpu_seconds(pid)
    time.sleep(seconds)
    used = _cpu_seconds(pid) - before
    return [Result(f"CPU at rest over {seconds:g} s", round(100 * used / seconds, 3), "%", 0.05)]


BENCHES: dict[str, Callable[[Bench], list[Result]]] = {
    "start": bench_start,
    "calls": bench_calls,
    "stop": bench_stop,
    "rest": bench_rest,
    "fast": bench_fast_writer,
    "hostile": bench_hostile,
    "combining": bench_combining,
    "reads": bench_reads,
    "wakeups": bench_wakeups,
}


def run(names: list[str], cpu_seconds: float) -> list[Result]:
    results = []
    for name in names:
        if name == "cpu" and cpu_seconds <= 0:
            continue
        b = Bench()
        try:
            if name == "cpu":
                results += bench_cpu(b, cpu_seconds)
            else:
                results += BENCHES[name](b)
        finally:
            b.close()
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", help="comma-separated: " + ",".join([*BENCHES, "cpu"]))
    parser.add_argument("--cpu-seconds", type=float, default=300.0,
                        help="how long to sample CPU at rest (default 300); 0 skips it")
    args = parser.parse_args()
    names = args.only.split(",") if args.only else [*BENCHES] + (
        ["cpu"] if args.cpu_seconds > 0 else []
    )
    factor = float(os.environ.get("BURNER_BENCH_FACTOR", "1"))
    results = run(names, args.cpu_seconds)
    print(f"{'measure':<36} {'value':>10} {'budget':>10}  unit")
    for r in results:
        value = "n/a" if r.value is None else f"{r.value:g}"
        mark = "  OVER" if r.over(factor) else ""
        print(f"{r.name:<36} {value:>10} {r.budget * factor:>10g}  {r.unit}{mark}")
    return 1 if any(r.over(factor) for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
