from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

PUBLISH_SCRIPT = Path(__file__).parents[1] / "scripts" / "publish-release"


def git(repository: Path, *arguments: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=check,
        capture_output=True,
        text=True,
    )


def create_repository(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(remote)],
        check=True,
    )
    repository.mkdir()
    (repository / "scripts").mkdir()
    shutil.copy2(PUBLISH_SCRIPT, repository / "scripts" / "publish-release")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tfr"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "TFR Tests")
    git(repository, "config", "user.email", "tfr@example.invalid")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "fixture")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "--quiet", "--set-upstream", "origin", "main")
    return repository, remote


def publish(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(repository / "scripts" / "publish-release"), *arguments],
        check=False,
        capture_output=True,
        cwd=repository.parent,
        text=True,
    )


def test_preview_validates_without_creating_tag(tmp_path: Path) -> None:
    repository, remote = create_repository(tmp_path)

    result = publish(repository)

    assert result.returncode == 0
    assert "Release check passed: v0.1.0" in result.stdout
    assert "--push" in result.stdout
    assert git(repository, "tag", "--list", "v0.1.0").stdout == ""
    assert git(remote, "tag", "--list").stdout == ""


def test_push_creates_annotated_local_and_remote_tag(tmp_path: Path) -> None:
    repository, remote = create_repository(tmp_path)
    git(repository, "tag", "-a", "v0.0.9", "-m", "Unrelated local tag")
    git(repository, "config", "push.followTags", "true")
    git(
        repository,
        "config",
        "--add",
        "remote.origin.push",
        "refs/heads/main:refs/heads/release-shadow",
    )
    main_before = git(remote, "rev-parse", "refs/heads/main").stdout.strip()

    result = publish(repository, "--push")

    assert result.returncode == 0
    assert "protected GitHub release workflow" in result.stdout
    assert git(repository, "cat-file", "-t", "refs/tags/v0.1.0").stdout.strip() == "tag"
    assert git(repository, "rev-list", "-n", "1", "v0.1.0").stdout.strip() == git(
        repository, "rev-parse", "HEAD"
    ).stdout.strip()
    assert git(remote, "show-ref", "--verify", "refs/tags/v0.1.0").returncode == 0
    assert git(remote, "tag", "--list", "v0.0.9").stdout == ""
    assert git(remote, "rev-parse", "refs/heads/main").stdout.strip() == main_before
    assert (
        git(
            remote,
            "show-ref",
            "--verify",
            "refs/heads/release-shadow",
            check=False,
        ).returncode
        != 0
    )


def test_rejects_dirty_checkout(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    (repository / "untracked.txt").write_text("dirty\n", encoding="utf-8")

    result = publish(repository)

    assert result.returncode == 2
    assert "modified, staged, or untracked" in result.stderr


def test_rejects_non_main_branch(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    git(repository, "switch", "--quiet", "-c", "feature")

    result = publish(repository)

    assert result.returncode == 2
    assert "main branch" in result.stderr


def test_rejects_unpushed_commit(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    (repository / "note.txt").write_text("local\n", encoding="utf-8")
    git(repository, "add", "note.txt")
    git(repository, "commit", "--quiet", "-m", "local commit")

    result = publish(repository)

    assert result.returncode == 2
    assert "have not been pushed" in result.stderr


def test_rejects_branch_behind_remote(tmp_path: Path) -> None:
    repository, remote = create_repository(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    git(other, "config", "user.name", "TFR Tests")
    git(other, "config", "user.email", "tfr@example.invalid")
    (other / "note.txt").write_text("remote\n", encoding="utf-8")
    git(other, "add", "note.txt")
    git(other, "commit", "--quiet", "-m", "remote commit")
    git(other, "push", "--quiet", "origin", "main")

    result = publish(repository)

    assert result.returncode == 2
    assert "behind origin/main" in result.stderr


def test_rejects_diverged_branch(tmp_path: Path) -> None:
    repository, remote = create_repository(tmp_path)
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "--quiet", str(remote), str(other)], check=True)
    git(other, "config", "user.name", "TFR Tests")
    git(other, "config", "user.email", "tfr@example.invalid")
    (other / "remote.txt").write_text("remote\n", encoding="utf-8")
    git(other, "add", "remote.txt")
    git(other, "commit", "--quiet", "-m", "remote commit")
    git(other, "push", "--quiet", "origin", "main")
    (repository / "local.txt").write_text("local\n", encoding="utf-8")
    git(repository, "add", "local.txt")
    git(repository, "commit", "--quiet", "-m", "local commit")

    result = publish(repository)

    assert result.returncode == 2
    assert "diverged from origin/main" in result.stderr


def test_accepts_matching_existing_remote_tag_as_completed(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    git(repository, "tag", "-a", "v0.1.0", "-m", "Release v0.1.0")
    git(repository, "push", "--quiet", "origin", "refs/tags/v0.1.0")

    result = publish(repository, "--push")

    assert result.returncode == 0
    assert "already exists on the verified remote" in result.stdout


def test_rejects_distinct_push_url(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    other = tmp_path / "other.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(other)],
        check=True,
    )
    git(repository, "remote", "set-url", "--push", "origin", str(other))

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "fetch and push URLs must be identical" in result.stderr
    assert git(other, "tag", "--list").stdout == ""


def test_rejects_multiple_push_urls(tmp_path: Path) -> None:
    repository, remote = create_repository(tmp_path)
    other = tmp_path / "other.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(other)],
        check=True,
    )
    git(repository, "remote", "set-url", "--add", "--push", "origin", str(remote))
    git(repository, "remote", "set-url", "--add", "--push", "origin", str(other))

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "exactly one push URL" in result.stderr
    assert git(remote, "tag", "--list").stdout == ""
    assert git(other, "tag", "--list").stdout == ""


def test_rejects_multiple_fetch_urls(tmp_path: Path) -> None:
    repository, remote = create_repository(tmp_path)
    other = tmp_path / "other.git"
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(other)],
        check=True,
    )
    git(repository, "remote", "set-url", "--add", "origin", str(other))

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "exactly one fetch URL" in result.stderr
    assert git(remote, "tag", "--list").stdout == ""
    assert git(other, "tag", "--list").stdout == ""


def test_rejects_conflicting_remote_tag(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    git(repository, "tag", "-a", "v0.1.0", "-m", "Conflicting release")
    git(repository, "push", "--quiet", "origin", "refs/tags/v0.1.0")
    git(repository, "tag", "--delete", "v0.1.0")

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "remote tag already exists with different content" in result.stderr


def test_rejects_nested_release_tag(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    git(repository, "tag", "-a", "base", "-m", "Base tag")
    git(repository, "tag", "-a", "v0.1.0", "-m", "Nested tag", "base")

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "does not point directly to a commit" in result.stderr


def test_replacement_object_cannot_hide_wrong_tag_target(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    first_commit = git(repository, "rev-parse", "HEAD").stdout.strip()
    (repository / "second.txt").write_text("second\n", encoding="utf-8")
    git(repository, "add", "second.txt")
    git(repository, "commit", "--quiet", "-m", "second commit")
    git(repository, "push", "--quiet", "origin", "main")
    git(repository, "tag", "-a", "v0.1.0", "-m", "Wrong target", first_commit)
    git(repository, "tag", "-a", "replacement", "-m", "Replacement target", "HEAD")
    candidate_object = git(repository, "rev-parse", "refs/tags/v0.1.0").stdout.strip()
    replacement_object = git(
        repository, "rev-parse", "refs/tags/replacement"
    ).stdout.strip()
    git(repository, "replace", candidate_object, replacement_object)

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "local release tag points to another commit" in result.stderr


def test_rejects_version_older_than_existing_stable_tag(tmp_path: Path) -> None:
    repository, _remote = create_repository(tmp_path)
    git(repository, "tag", "-a", "v0.2.0", "-m", "Future release")
    git(repository, "push", "--quiet", "origin", "refs/tags/v0.2.0")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tfr"\nversion = "0.1.1"\n',
        encoding="utf-8",
    )
    git(repository, "add", "pyproject.toml")
    git(repository, "commit", "--quiet", "-m", "older version")
    git(repository, "push", "--quiet", "origin", "main")

    result = publish(repository, "--push")

    assert result.returncode == 2
    assert "must advance the stable channel" in result.stderr
