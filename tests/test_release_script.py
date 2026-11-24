"""Behaviour of .github/scripts/draft-release.sh against a stubbed ``gh``.

The stub keeps the repository's releases, tags and workflow runs in a small JSON
state directory and runs the script's own ``--jq`` filters over it, so the real
filters are exercised.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "draft-release.sh"
SHA = "0123456789abcdef0123456789abcdef01234567"
VERSION = "2031.1.2.3"

pytestmark = pytest.mark.skipif(
    shutil.which("bash") is None or shutil.which("jq") is None,
    reason="bash and jq are required",
)

GH_STUB = r"""#!/usr/bin/env bash
set -euo pipefail
S="$STUB_STATE"
echo "gh $*" >> "$S/calls.log"
jqexpr=""
args=("$@")
for ((i = 0; i < ${#args[@]}; i++)); do
  [[ "${args[i]}" == "--jq" ]] && jqexpr="${args[i + 1]}"
done
emit() { if [[ -n "$jqexpr" ]]; then jq -r "$jqexpr"; else cat; fi; }
case "$1 $2" in
  "run list")
    wf=""
    for ((i = 0; i < ${#args[@]}; i++)); do
      [[ "${args[i]}" == "--workflow" ]] && wf="${args[i + 1]}"
    done
    if [[ -f "$S/runs/$wf" ]]; then cat "$S/runs/$wf"; else echo '[]'; fi | emit ;;
  "api repos/"*)
    case "$2" in
      */git/matching-refs/tags/*) cat "$S/tags.json" | emit ;;
      */releases) cat "$S/releases.json" | emit ;;
    esac ;;
  "api --paginate") cat "$S/releases.json" | emit ;;
  "api --method")
    body=""
    declare -A f
    for ((i = 0; i < ${#args[@]}; i++)); do
      case "${args[i]}" in
        -f) k="${args[i + 1]%%=*}"; f[$k]="${args[i + 1]#*=}" ;;
        -F)
          k="${args[i + 1]%%=*}"; v="${args[i + 1]#*=}"
          if [[ "$v" == @* ]]; then body="$(cat "${v#@}")"; else f[$k]="$v"; fi ;;
      esac
    done
    rel="$(jq -n --arg t "${f[tag_name]}" --arg n "${f[name]}" --arg c "${f[target_commitish]}" \
      --arg b "$body" --argjson d "${f[draft]}" \
      '{tag_name:$t, name:$n, target_commitish:$c, body:$b, draft:$d}')"
    jq --argjson r "$rel" '. + [$r]' "$S/releases.json" > "$S/releases.tmp"
    mv "$S/releases.tmp" "$S/releases.json"
    echo "$rel" | emit ;;
  *) echo "unexpected gh call: $*" >&2; exit 99 ;;
esac
"""

CHANGELOG = f"""# Changelog

## 2031.1.2.30 - 2031-01-03

- The release after the one under test.

## {VERSION} - 2031-01-02

### Added
- The thing under test.

### Fixed
- Another thing.

## 2031.1.1.1 - 2031-01-01

- The previous release.
"""


class Env:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.state = root / "state"
        self.repo = root / "repo"
        (self.state / "runs").mkdir(parents=True)
        (self.repo / "custom_components" / "climate_adapt").mkdir(parents=True)
        (self.root / "bin").mkdir()
        gh = self.root / "bin" / "gh"
        gh.write_text(GH_STUB)
        gh.chmod(0o755)
        (self.state / "tags.json").write_text("[]")
        (self.state / "releases.json").write_text("[]")
        self.set_manifest(VERSION)
        self.set_changelog(CHANGELOG)
        for wf in ("test.yml", "hassfest.yml", "hacs.yml"):
            self.set_run(wf, "completed", "success")

    def set_manifest(self, version: str) -> None:
        path = self.repo / "custom_components" / "climate_adapt" / "manifest.json"
        path.write_text(json.dumps({"domain": "climate_adapt", "version": version}))

    def set_changelog(self, text: str) -> None:
        (self.repo / "CHANGELOG.md").write_text(text)

    def set_run(self, wf: str, status: str, conclusion: str | None) -> None:
        run = [{"status": status, "conclusion": conclusion or ""}]
        (self.state / "runs" / wf).write_text(json.dumps(run))

    def clear_run(self, wf: str) -> None:
        (self.state / "runs" / wf).unlink()

    def set_releases(self, releases: list[dict]) -> None:
        (self.state / "releases.json").write_text(json.dumps(releases))

    def set_tags(self, tags: list[str]) -> None:
        refs = [{"ref": f"refs/tags/{t}"} for t in tags]
        (self.state / "tags.json").write_text(json.dumps(refs))

    def releases(self) -> list[dict]:
        return json.loads((self.state / "releases.json").read_text())

    def run(self) -> subprocess.CompletedProcess[str]:
        env = {
            "PATH": f"{self.root / 'bin'}{os.pathsep}{os.environ['PATH']}",
            "HOME": str(self.root),
            "STUB_STATE": str(self.state),
            "GH_REPO": "owner/repo",
            "TARGET_SHA": SHA,
        }
        return subprocess.run(  # noqa: S603
            ["bash", str(SCRIPT)],  # noqa: S607
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@pytest.fixture
def env(tmp_path: Path) -> Env:
    return Env(tmp_path)


def test_bump_creates_one_draft_without_a_tag(env: Env) -> None:
    result = env.run()
    assert result.returncode == 0, result.stderr
    (release,) = env.releases()
    assert release["draft"] is True
    assert release["tag_name"] == f"v{VERSION}"
    assert release["name"] == f"v{VERSION}"
    assert release["target_commitish"] == SHA
    assert release["body"].startswith("### Added")
    assert "The thing under test." in release["body"]
    assert "Another thing." in release["body"]
    assert "release after the one under test" not in release["body"]
    assert "previous release" not in release["body"]
    calls = (env.state / "calls.log").read_text()
    assert "git/refs" not in calls
    assert "release create" not in calls


def test_rerun_on_the_same_commit_creates_no_second_draft(env: Env) -> None:
    assert env.run().returncode == 0
    assert env.run().returncode == 0
    assert len(env.releases()) == 1


@pytest.mark.parametrize("draft", [True, False])
def test_existing_release_means_nothing_to_do(env: Env, draft: bool) -> None:
    env.set_releases([{"tag_name": f"v{VERSION}", "name": f"v{VERSION}", "draft": draft}])
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert len(env.releases()) == 1
    assert "already exists" in result.stdout


def test_existing_tag_means_nothing_to_do(env: Env) -> None:
    env.set_tags([f"v{VERSION}"])
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert env.releases() == []
    assert "Tag" in result.stdout


def test_a_longer_version_with_the_same_prefix_is_not_a_match(env: Env) -> None:
    env.set_tags([f"v{VERSION}0"])
    env.set_releases([{"tag_name": f"v{VERSION}0", "name": f"v{VERSION}0", "draft": False}])
    assert env.run().returncode == 0
    assert len(env.releases()) == 2


def test_no_bump_after_a_release_creates_nothing(env: Env) -> None:
    assert env.run().returncode == 0
    env.set_tags([f"v{VERSION}"])
    assert env.run().returncode == 0
    assert len(env.releases()) == 1


def test_missing_changelog_section_fails(env: Env) -> None:
    env.set_manifest("2031.1.3.1")
    result = env.run()
    assert result.returncode == 1
    assert "no section" in result.stdout
    assert env.releases() == []


def test_empty_changelog_section_fails(env: Env) -> None:
    empty = f"# Changelog\n\n## {VERSION} - 2031-01-02\n\n## 2031.1.1.1 - 2031-01-01\n\n- x\n"
    env.set_changelog(empty)
    assert env.run().returncode == 1
    assert env.releases() == []


def test_bracketed_changelog_heading_is_accepted(env: Env) -> None:
    env.set_changelog(
        f"# Changelog\n\n## [{VERSION}] - 2031-01-02\n\n- Bracketed.\n\n"
        "## [2031.1.1.1] - x\n\n- Old.\n"
    )
    assert env.run().returncode == 0
    (release,) = env.releases()
    assert release["body"].strip() == "- Bracketed."


@pytest.mark.parametrize("version", ["", "v2031.1.2.3", "2031.1.2", "1.0.0", "2031.1.2.3; id"])
def test_malformed_manifest_version_fails(env: Env, version: str) -> None:
    env.set_manifest(version)
    assert env.run().returncode == 1
    assert env.releases() == []


@pytest.mark.parametrize(
    ("status", "conclusion"),
    [("completed", "failure"), ("completed", "cancelled"), ("in_progress", None), ("queued", None)],
)
@pytest.mark.parametrize("wf", ["test.yml", "hassfest.yml", "hacs.yml"])
def test_unfinished_or_red_checks_never_release(
    env: Env, wf: str, status: str, conclusion: str | None
) -> None:
    env.set_run(wf, status, conclusion)
    result = env.run()
    assert result.returncode == 0, result.stderr
    assert env.releases() == []


def test_a_check_with_no_run_never_releases(env: Env) -> None:
    env.clear_run("hacs.yml")
    assert env.run().returncode == 0
    assert env.releases() == []
