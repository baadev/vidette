"""`.github/scripts/release-channel.sh`: which pushed tags move the `latest` image tag.

The release workflow trusts this script for the one decision that can hurt users who run
`:latest`: a release candidate must never become `latest`, and neither may a hotfix on an
older line pushed after a newer stable release.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "scripts" / "release-channel.sh"

pytestmark = pytest.mark.skipif(
    shutil.which("git") is None or shutil.which("bash") is None, reason="needs git and bash"
)


def _repo(tmp_path: Path, tags: list[str]) -> Path:
    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    for tag in tags:
        git("tag", tag)
    return tmp_path


def _channel(repo: Path, tag: str) -> dict[str, str]:
    result = subprocess.run(
        ["bash", str(SCRIPT), tag], cwd=repo, check=True, capture_output=True, text=True
    )
    return dict(line.split("=", 1) for line in result.stdout.splitlines())


HISTORY = ["nvr-core", "v0.1.0", "v0.1.1", "v0.1.2", "v0.1.3"]


@pytest.mark.parametrize(
    ("existing", "tag", "prerelease", "latest"),
    [
        (HISTORY, "v0.2.0-rc.1", "true", "false"),  # a candidate never moves latest
        (HISTORY, "v0.2.0-beta", "true", "false"),
        ([*HISTORY, "v0.2.0-rc.1"], "v0.2.0", "false", "true"),  # the stable release does
        ([*HISTORY, "v0.2.0"], "v0.2.0", "false", "true"),  # tag already fetched in CI
        ([*HISTORY, "v0.2.0"], "v0.1.4", "false", "false"),  # old-line hotfix: stays put
        ([*HISTORY, "v0.9.0"], "v0.10.0", "false", "true"),  # numeric, not lexical, order
        ([], "v0.1.0", "false", "true"),  # first release ever
        (HISTORY, "vnext", "true", "false"),  # not semver at all: never latest
    ],
)
def test_latest_moves_only_to_the_newest_stable_tag(
    tmp_path: Path, existing: list[str], tag: str, prerelease: str, latest: str
) -> None:
    channel = _channel(_repo(tmp_path, existing), tag)
    assert channel == {"prerelease": prerelease, "latest": latest}


def test_workflow_wires_the_script_into_the_latest_tag() -> None:
    workflow = (SCRIPT.parents[1] / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "release-channel.sh" in workflow
    assert "fetch-depth: 0" in workflow  # the script needs every tag
    assert "latest=false" in workflow  # metadata-action's own latest rule is off
    assert "type=raw,value=latest,enable=${{ steps.channel.outputs.latest }}" in workflow
    assert "type=raw,value=latest\n" not in workflow  # the old unconditional tag is gone
    assert "prerelease: ${{ needs.image.outputs.prerelease }}" in workflow
