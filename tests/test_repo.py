"""The CI workflow and the pinned tools follow the repository's supply chain rules."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text()


def test_actions_are_pinned_to_full_commits() -> None:
    uses = re.findall(r"uses:\s*(\S+)", CI)
    assert uses
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_workflow_limits() -> None:
    assert re.search(r"(?m)^permissions:\n  contents: read$", CI)
    assert "persist-credentials: false" in CI
    assert re.search(r"timeout-minutes: 15", CI)
    triggers = re.search(r"(?ms)^on:\n(.*?)^\S", CI).group(1)
    assert re.findall(r"(?m)^  (\w+):", triggers) == ["push", "pull_request"]


def test_matrix() -> None:
    assert '["3.11", "3.13", "3.15"]' in CI
    assert "-c constraints.txt" in CI


def test_tools_are_pinned_exactly() -> None:
    pins = (ROOT / "constraints.txt").read_text().split()
    assert {p.split("==")[0] for p in pins} == {"ruff", "pytest", "pytest-timeout"}
    assert all(re.fullmatch(r"[\w-]+==[\w.]+", p) for p in pins)


def test_dependabot_updates_the_actions() -> None:
    config = (ROOT / ".github" / "dependabot.yml").read_text()
    assert 'package-ecosystem: "github-actions"' in config


def run_dir_docs() -> str:
    readme = (ROOT / "README.md").read_text()
    return readme[readme.index("## Files in the run dir") :]


def test_the_readme_describes_every_file_and_end() -> None:
    docs = run_dir_docs()
    for suffix in (".lock", ".json", ".sock", ".log", ".log.1", ".exit"):
        assert f"`NAME{suffix}`" in docs, suffix
    for heading in ("## Lifecycle", "## Stopping", "## Liveness", "## Protocol and versions",
                    "## Limits and non-goals"):
        assert heading in docs
    for row in ("SIGKILL or crashes", "run dir is deleted", "A reboot", "fails"):
        assert row in docs
    assert "burner clean [NAME]" in docs and "UnsafeRunDirError" in docs


def test_security_policy() -> None:
    policy = (ROOT / "SECURITY.md").read_text()
    for part in ("## Trust model", "## Supported versions", "Report a vulnerability"):
        assert part in policy


def test_no_em_dash_in_the_docs() -> None:
    for name in ("README.md", "SECURITY.md", "CHANGELOG.md"):
        assert "—" not in (ROOT / name).read_text(), name
