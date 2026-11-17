from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

from conftest import _cleanup

import burner
from burner import Host
from burner.cli import _parser

ROOT = Path(__file__).resolve().parents[1]
README = (ROOT / "README.md").read_text()


def python_block(text: str) -> str:
    match = re.search(r"```python\n(.*?)```", text, re.S)
    assert match
    return match[1]


def test_readme_python_block_runs(run_dir: Path) -> None:
    bin_dir = str(Path(sys.executable).parent)
    env = {**os.environ, "BURNER_DIR": str(run_dir),
           "PATH": bin_dir + os.pathsep + os.environ.get("PATH", "")}
    try:
        result = subprocess.run([sys.executable, "-c", python_block(README)], env=env,
                                cwd=run_dir, capture_output=True, text=True, timeout=60)
    finally:
        _cleanup(run_dir)
    assert result.returncode == 0, result.stderr
    assert "ExitStatus(code=0" in result.stdout
    assert "hello from a log session" in result.stdout


def test_readme_names_every_public_name() -> None:
    for name in burner.__all__:
        assert f"`{name}" in README or f"{name}(" in README, name
    methods = [m for m in dir(Host) if not m.startswith("_")]
    for method in methods:
        assert f"`{method}(" in README, method
    for name in ("Session", "ExitStatus"):
        for field in getattr(burner, name).__dataclass_fields__:
            assert f"`{field}`" in README, field


def test_readme_names_every_command() -> None:
    sub = next(a for a in _parser()._actions if a.dest == "command")
    for command in sub.choices:
        assert f"burner {command}" in README, command


def test_install_line() -> None:
    assert (f'pip install "burner @ git+https://github.com/CakeBrewery/burner.git'
            f'@v{burner.__version__}"') in README
    assert "`pip install burner` installs an unrelated project" in README


def test_no_em_dash() -> None:
    files = [*ROOT.glob("*.md"), *ROOT.glob("src/**/*.py"), *ROOT.glob("tools/*.py")]
    assert files
    for path in files:
        assert "—" not in path.read_text(), path


def test_changelog_has_the_version() -> None:
    changelog = (ROOT / "CHANGELOG.md").read_text()
    assert f"## [{burner.__version__}]" in changelog


def test_every_public_member_has_a_docstring() -> None:
    for name in burner.__all__:
        obj = getattr(burner, name)
        assert obj.__doc__ and obj.__doc__.strip(), name
    for method in (m for m in dir(Host) if not m.startswith("_")):
        assert getattr(Host, method).__doc__, method
