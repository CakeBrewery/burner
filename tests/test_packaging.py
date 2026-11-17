from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import burner

ROOT = Path(__file__).resolve().parents[1]
BUILD = """
import sys
from setuptools import build_meta
print(build_meta.build_wheel(sys.argv[1]))
"""


@pytest.fixture(scope="module")
def wheel(tmp_path_factory: pytest.TempPathFactory) -> zipfile.ZipFile:
    tree = tmp_path_factory.mktemp("tree")
    for name in ("pyproject.toml", "README.md", "LICENSE"):
        shutil.copy(ROOT / name, tree / name)
    shutil.copytree(ROOT / "src", tree / "src",
                    ignore=shutil.ignore_patterns("*.egg-info", "__pycache__"))
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run([sys.executable, "-c", BUILD, str(out)], cwd=tree,
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    name = result.stdout.strip().splitlines()[-1]
    with zipfile.ZipFile(out / name) as zf:
        yield zf


def read(wheel: zipfile.ZipFile, suffix: str) -> str:
    [name] = [n for n in wheel.namelist() if n.endswith(suffix)]
    return wheel.read(name).decode()


def test_wheel_holds_py_typed(wheel: zipfile.ZipFile) -> None:
    assert "burner/py.typed" in wheel.namelist()


def test_wheel_license(wheel: zipfile.ZipFile) -> None:
    metadata = read(wheel, ".dist-info/METADATA")
    assert "License-Expression: MIT" in metadata
    assert "License-File: LICENSE" in metadata
    assert "MIT License" in read(wheel, ".dist-info/licenses/LICENSE")


def test_wheel_metadata(wheel: zipfile.ZipFile) -> None:
    metadata = read(wheel, ".dist-info/METADATA")
    assert f"Version: {burner.__version__}" in metadata
    assert "Requires-Python: >=3.11" in metadata
    assert "Project-URL: Repository, https://github.com/CakeBrewery/burner" in metadata
    assert "Classifier: Typing :: Typed" in metadata


def test_wheel_entry_point(wheel: zipfile.ZipFile) -> None:
    entry_points = read(wheel, ".dist-info/entry_points.txt")
    assert "[console_scripts]" in entry_points
    assert "burner = burner.cli:main" in entry_points


def test_version() -> None:
    assert burner.__version__ == "0.2.0"
