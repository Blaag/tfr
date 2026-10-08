from __future__ import annotations

import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

RELEASE_SCRIPT = Path(__file__).parents[1] / "scripts" / "release-end-to-end"
AGENT_RELEASE_SCRIPT = Path(__file__).parents[1] / "scripts" / "release-end-to-end-agent"


def git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def executable(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)


def create_fixture(
    tmp_path: Path,
    *,
    security_failure: bool = False,
    codeql_failure: bool = False,
) -> tuple[Path, dict[str, str]]:
    remote = tmp_path / "remote.git"
    repository = tmp_path / "repository"
    fake_bin = tmp_path / "bin"
    state = tmp_path / "state"
    repository.mkdir()
    fake_bin.mkdir()
    state.mkdir()
    (repository / "scripts").mkdir()
    (repository / "src" / "tfr" / "web").mkdir(parents=True)
    (repository / "tests" / "e2e").mkdir(parents=True)
    shutil.copy2(RELEASE_SCRIPT, repository / "scripts" / "release-end-to-end")
    (repository / "scripts" / "release-end-to-end").chmod(0o755)
    shutil.copy2(AGENT_RELEASE_SCRIPT, repository / "scripts" / "release-end-to-end-agent")
    (repository / "scripts" / "release-end-to-end-agent").chmod(0o755)
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tfr"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (repository / "uv.lock").write_text('version = "0.1.0"\n', encoding="utf-8")
    (repository / "src" / "tfr" / "web" / "app.mjs").write_text("\n", encoding="utf-8")
    executable(
        repository / "scripts" / "check-security",
        "#!/bin/sh\n"
        "printf '%s\\n' security >>\"$TFR_FAKE_LOG\"\n"
        f"exit {7 if security_failure else 0}\n",
    )
    executable(
        repository / "scripts" / "publish-release",
        """#!/bin/sh
printf 'publish %s\n' "$*" >>"$TFR_FAKE_LOG"
printf 'Release check passed\n'
""",
    )
    subprocess.run(
        ["git", "init", "--bare", "--quiet", "--initial-branch=main", str(remote)],
        check=True,
    )
    git(repository, "init", "--quiet", "--initial-branch=main")
    git(repository, "config", "user.name", "TFR Tests")
    git(repository, "config", "user.email", "tfr@example.invalid")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "initial")
    git(repository, "remote", "add", "origin", str(remote))
    git(repository, "push", "--quiet", "--set-upstream", "origin", "main")
    git(repository, "switch", "--quiet", "-c", "feature")
    (repository / "feature.txt").write_text("feature\n", encoding="utf-8")
    git(repository, "add", "feature.txt")
    git(repository, "commit", "--quiet", "-m", "feature")

    executable(
        fake_bin / "uv",
        r'''#!/usr/bin/env python3
import os
import re
import sys
from pathlib import Path

args = sys.argv[1:]
with Path(os.environ["TFR_FAKE_LOG"]).open("a", encoding="utf-8") as output:
    output.write("uv " + " ".join(args) + "\n")

if args[:2] == ["version", "--short"]:
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    print(re.search(r'^version = "([^"]*)"$', text, re.MULTILINE).group(1))
elif args and args[0] == "version" and len(args) > 1:
    version = args[1]
    path = Path("pyproject.toml")
    updated = re.sub(
        r'^version = "[^"]*"$',
        f'version = "{version}"',
        path.read_text(),
        flags=re.MULTILINE,
    )
    path.write_text(
        updated,
        encoding="utf-8",
    )
    Path("uv.lock").write_text(f'version = "{version}"\n', encoding="utf-8")
elif args and args[0] == "build":
    target = Path(args[args.index("--out-dir") + 1])
    target.mkdir(parents=True, exist_ok=True)
    (target / "fake.whl").touch()
''',
    )
    for command in ("node", "npm"):
        executable(
            fake_bin / command,
            f'#!/bin/sh\nprintf \'{command} %s\\n\' "$*" >>"$TFR_FAKE_LOG"\n',
        )
    executable(
        fake_bin / "gh",
        r'''#!/usr/bin/env python3
import json
import os
import subprocess
import sys
from pathlib import Path

args = sys.argv[1:]
state = Path(os.environ["TFR_FAKE_STATE"])
log = Path(os.environ["TFR_FAKE_LOG"])
codeql_failure = os.environ.get("TFR_FAKE_CODEQL_FAILURE") == "1"
with log.open("a", encoding="utf-8") as output:
    output.write("gh " + " ".join(args) + "\n")

def value(flag):
    return args[args.index(flag) + 1]

def output(value):
    if not isinstance(value, str):
        value = json.dumps(value)
    print(value)

if args[:2] == ["pr", "view"] and "--arg" in args:
    raise SystemExit("gh pr view does not accept jq --arg options")

if args[:2] == ["auth", "status"]:
    raise SystemExit(0)
if args[:2] == ["repo", "view"]:
    output("Blaag/tfr")
elif args[:2] == ["pr", "list"]:
    if os.environ.get("TFR_FAKE_RESUME") == "1" and value("--head").startswith("release/"):
        output([{"number": 2}])
    else:
        output([])
elif args[:2] == ["pr", "create"]:
    number = "2" if value("--head").startswith("release/") else "1"
    output(f"https://github.invalid/Blaag/tfr/pull/{number}")
elif args[:2] == ["pr", "view"]:
    target = args[2]
    number = target.rsplit("/", 1)[-1] if "/" in target else target
    fields = value("--json")
    if fields == "number":
        output(number)
    elif fields == "baseRefName,isDraft,state":
        print("true")
    elif fields == "baseRefName,headRefName,isDraft,state":
        output({
            "baseRefName": "main",
            "headRefName": "release/v0.1.1",
            "isDraft": False,
            "state": "OPEN",
        })
    elif fields == "headRefOid":
        if os.environ.get("TFR_FAKE_RESUME") == "1":
            calls = state / "resume-head-calls"
            count = int(calls.read_text() or "0") if calls.exists() else 0
            calls.write_text(str(count + 1), encoding="utf-8")
            original = (state / "resume-original-head").read_text(encoding="utf-8")
            if count < 2:
                output(original)
            else:
                output(
                    subprocess.check_output(
                        ["git", "rev-parse", "origin/release/v0.1.1"], text=True
                    ).strip()
                )
        else:
            output(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip())
    elif fields == "files":
        files = [{"path": "pyproject.toml"}, {"path": "uv.lock"}]
        if os.environ.get("TFR_FAKE_UNEXPECTED_RELEASE_FILE") == "1":
            files.append({"path": "unexpected.txt"})
        if "--jq" in args:
            print("\n".join(sorted(item["path"] for item in files)))
        else:
            output({"files": files})
    elif fields == "mergedAt":
        print("true" if (state / f"merged-{number}").exists() else "false")
    else:
        raise SystemExit(f"unsupported pr view fields: {fields}")
elif args[:2] == ["pr", "checks"]:
    registration = state / f"checks-{args[2]}"
    if not registration.exists():
        registration.touch()
        print(f"no checks reported on the '{args[2]}' branch", file=sys.stderr)
        raise SystemExit(1)
    checks = [
        {"name": "CodeQL", "workflow": "", "bucket": "pass", "state": "SUCCESS"},
        {"name": "Analyze (actions)", "workflow": "CodeQL", "bucket": "pass", "state": "SUCCESS"},
        {
            "name": "Analyze (javascript-typescript)",
            "workflow": "CodeQL",
            "bucket": "pass",
            "state": "SUCCESS",
        },
        {"name": "Analyze (python)", "workflow": "CodeQL", "bucket": "pass", "state": "SUCCESS"},
        {"name": "test", "workflow": "CI", "bucket": "pass", "state": "SUCCESS"},
        {
            "name": "dependency-review",
            "workflow": "Dependency Review",
            "bucket": "pass",
            "state": "SUCCESS",
        },
    ]
    if codeql_failure:
        for check in checks:
            if check["name"] == "Analyze (python)":
                check["bucket"] = "fail"
                check["state"] = "FAILURE"
    if "--json" in args:
        output(checks)
    if codeql_failure and "--watch" in args:
        raise SystemExit(1)
elif args[:2] == ["pr", "merge"]:
    number = args[2]
    head = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    expected = value("--match-head-commit")
    if head != expected:
        raise SystemExit("head changed")
    tracking = subprocess.check_output(
        ["git", "rev-parse", "refs/remotes/origin/main"], text=True
    ).strip()
    parent = subprocess.check_output(
        ["git", "ls-remote", "origin", "refs/heads/main"], text=True
    ).split()[0]
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], text=True).strip()
    commit = subprocess.check_output(
        ["git", "commit-tree", tree, "-p", parent, "-m", f"merge PR {number}"], text=True
    ).strip()
    subprocess.run(["git", "push", "--quiet", "origin", f"{commit}:refs/heads/main"], check=True)
    subprocess.run(
        ["git", "update-ref", "refs/remotes/origin/main", tracking], check=True
    )
    (state / f"merged-{number}").touch()
elif args[:2] == ["run", "list"]:
    workflow = value("--workflow")
    commit = value("--commit") if "--commit" in args else ""
    if workflow == "Release":
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        output([{"databaseId": 30, "headSha": commit, "status": "in_progress", "conclusion": None}])
    else:
        run_id = 10 if workflow == "CI" else 20
        output([{
            "databaseId": run_id,
            "headSha": commit,
            "status": "completed",
            "conclusion": "success",
        }])
elif args[:2] == ["run", "watch"]:
    raise SystemExit(0)
elif args[:2] == ["run", "view"]:
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    output({
        "status": "in_progress",
        "conclusion": None,
        "headSha": commit,
        "jobs": [{"name": "build", "status": "completed", "conclusion": "success"}],
    })
elif args[0] == "api":
    endpoint = next(
        item for item in args[1:] if item == "user" or item.startswith("repos/")
    )
    if endpoint == "user":
        output("Blaag")
    elif endpoint.endswith("/rulesets"):
        output([{"id": 1}, {"id": 2}])
    elif endpoint.endswith("/rulesets/1"):
        output({
            "target": "branch",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/heads/main"]}},
            "bypass_actors": [],
            "current_user_can_bypass": "never",
            "rules": [
                {"type": "pull_request"},
                {
                    "type": "required_status_checks",
                    "parameters": {
                        "required_status_checks": [
                            {"context": "test"},
                            {"context": "dependency-review"},
                        ]
                    },
                },
                {
                    "type": "code_scanning",
                    "parameters": {
                        "code_scanning_tools": [
                            {
                                "tool": "CodeQL",
                                "security_alerts_threshold": "medium_or_higher",
                                "alerts_threshold": "errors",
                            }
                        ]
                    },
                },
            ],
        })
    elif endpoint.endswith("/rulesets/2"):
        output({
            "target": "tag",
            "enforcement": "active",
            "conditions": {"ref_name": {"include": ["refs/tags/v*"]}},
            "bypass_actors": [],
            "current_user_can_bypass": "never",
            "rules": [
                {"type": "update"},
                {"type": "deletion"},
                {"type": "non_fast_forward"},
            ],
        })
    elif endpoint.endswith("/environments/release"):
        output({
            "protection_rules": [
                {
                    "type": "required_reviewers",
                    "prevent_self_review": False,
                    "reviewers": [
                        {"type": "User", "reviewer": {"login": "Blaag"}}
                    ],
                }
            ]
        })
    elif endpoint.endswith("/immutable-releases"):
        output({"enabled": True})
    elif "/check-runs?" in endpoint:
        check_calls = state / "check-run-calls"
        check_count = int(check_calls.read_text() or "0") if check_calls.exists() else 0
        check_calls.write_text(str(check_count + 1), encoding="utf-8")
        transient = os.environ.get("TFR_FAKE_TRANSIENT_NEUTRAL") == "1" and check_count == 0
        names = [
            "CodeQL",
            "Analyze (actions)",
            "Analyze (javascript-typescript)",
            "Analyze (python)",
            "test",
            "dependency-review",
        ]
        runs = [
            {
                "name": name,
                "status": "completed",
                "conclusion": (
                    "failure"
                    if codeql_failure and name == "Analyze (python)"
                    else "neutral"
                    if transient and name == "CodeQL"
                    else "success"
                ),
                "started_at": "2026-01-01T00:00:00Z",
            }
            for name in names
        ]
        output({"check_runs": runs})
    elif endpoint.endswith("/pending_deployments") and "--method" not in args:
        output([{
            "environment": {"id": 99, "name": "release"},
            "current_user_can_approve": True,
        }])
    elif endpoint.endswith("/pending_deployments"):
        (state / "approved").touch()
        output([])
    else:
        raise SystemExit(f"unsupported api endpoint: {endpoint}")
elif args[:2] == ["release", "list"]:
    if os.environ.get("TFR_FAKE_NO_CANDIDATE") == "1":
        output([])
    else:
        tag = "v0.1.1-candidate.1"
        if subprocess.run(
            ["git", "show-ref", "--verify", "--quiet", f"refs/tags/{tag}"]
        ).returncode != 0:
            subprocess.run(["git", "tag", "-a", tag, "-m", f"Release {tag}"], check=True)
            subprocess.run(["git", "push", "--quiet", "origin", f"refs/tags/{tag}"], check=True)
        output([{"tagName": tag, "isDraft": False, "isPrerelease": True}])
elif args[:2] == ["release", "view"]:
    if "--json" not in args:
        raise SystemExit(1)
    tag = args[2]
    version = tag[1:].split("-candidate.", 1)[0]
    output({
        "tagName": tag,
        "isDraft": False,
        "isPrerelease": "-candidate." in tag,
        "isImmutable": True,
        "assets": [
            {"name": f"tfr-{version}-py3-none-any.whl"},
            {"name": f"tfr-{version}.tar.gz"},
            {"name": "update-manifest.json"},
        ],
        "url": f"https://github.invalid/Blaag/tfr/releases/tag/{tag}",
    })
else:
    raise SystemExit("unsupported gh command: " + " ".join(args))
''',
    )
    environment = os.environ.copy()
    environment.update(
        {
            "PATH": f"{fake_bin}{os.pathsep}{environment['PATH']}",
            "TFR_FAKE_LOG": str(state / "commands.log"),
            "TFR_FAKE_STATE": str(state),
            "TFR_RELEASE_POLL_SECONDS": "0",
            "TFR_RELEASE_TIMEOUT_SECONDS": "30",
            "TFR_RELEASE_CONFIRM_TIMEOUT_SECONDS": "2",
            "TFR_FAKE_CODEQL_FAILURE": "1" if codeql_failure else "0",
        }
    )
    return repository, environment


def run_with_confirmations(
    repository: Path,
    environment: dict[str, str],
    confirmations: list[str],
    *,
    resume: bool = False,
) -> tuple[int, str]:
    terminal, slave = pty.openpty()
    process = subprocess.Popen(
        [
            str(repository / "scripts" / "release-end-to-end"),
            *(["--resume"] if resume else []),
            "0.1.1",
        ],
        cwd=repository,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    confirmation = 0
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            ready, _, _ = select.select([terminal], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(terminal, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    output.extend(chunk)
                    prompts = output.count(b"Type v0.1.1")
                    while confirmation < min(prompts, len(confirmations)):
                        os.write(terminal, confirmations[confirmation].encode() + b"\n")
                        confirmation += 1
            returncode = process.poll()
            if returncode is not None:
                return returncode, output.decode(errors="replace")
        process.kill()
        process.wait()
        raise AssertionError(f"release script timed out:\n{output.decode(errors='replace')}")
    finally:
        os.close(terminal)


def prepare_resume_fixture(repository: Path, environment: dict[str, str]) -> None:
    feature = git(repository, "rev-parse", "feature")
    git(repository, "switch", "--quiet", "main")
    git(repository, "cherry-pick", "--quiet", feature)
    git(repository, "push", "--quiet", "origin", "main")
    git(repository, "switch", "--quiet", "-c", "release/v0.1.1")
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tfr"\nversion = "0.1.1"\n', encoding="utf-8"
    )
    (repository / "uv.lock").write_text('version = "0.1.1"\n', encoding="utf-8")
    git(repository, "add", "pyproject.toml", "uv.lock")
    git(repository, "commit", "--quiet", "-m", "release: v0.1.1")
    git(repository, "push", "--quiet", "--set-upstream", "origin", "release/v0.1.1")
    (Path(environment["TFR_FAKE_STATE"]) / "resume-original-head").write_text(
        git(repository, "rev-parse", "HEAD"), encoding="utf-8"
    )
    git(repository, "switch", "--quiet", "main")
    environment["TFR_FAKE_RESUME"] = "1"


def test_release_end_to_end_runs_protected_pipeline(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)

    returncode, output = run_with_confirmations(
        repository, environment, ["v0.1.1", "v0.1.1"]
    )

    assert returncode == 0, output
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert log.count("security\n") == 2
    assert "gh pr merge 1" in log
    assert "gh pr merge 2" in log
    assert "publish \n" in log
    assert "publish --push\n" in log
    assert "--method POST" in log
    assert (Path(environment["TFR_FAKE_STATE"]) / "approved").exists()
    assert "Release published and verified" in output
    assert git(repository, "show", "origin/main:feature.txt") == "feature"


def test_release_end_to_end_publishes_candidate_before_stable_promotion(
    tmp_path: Path,
) -> None:
    repository, environment = create_fixture(tmp_path)
    environment["TFR_FAKE_NO_CANDIDATE"] = "1"

    returncode, output = run_with_confirmations(
        repository,
        environment,
        ["v0.1.1-candidate.1", "v0.1.1-candidate.1"],
    )

    assert returncode == 0, output
    assert "Test this candidate in the candidate channel" in output
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "publish --candidate 1\n" in log
    assert "publish --candidate 1 --push\n" in log
    assert "publish --push\n" not in log


def test_release_end_to_end_promotes_tested_candidate_from_clean_main(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)
    environment["TFR_FAKE_NO_CANDIDATE"] = "1"
    first_code, first_output = run_with_confirmations(
        repository,
        environment,
        ["v0.1.1-candidate.1", "v0.1.1-candidate.1"],
    )
    assert first_code == 0, first_output
    environment.pop("TFR_FAKE_NO_CANDIDATE")

    returncode, output = run_with_confirmations(
        repository,
        environment,
        ["v0.1.1", "v0.1.1"],
    )

    assert returncode == 0, output
    assert "Release published and verified" in output
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert log.count("gh pr merge") == 2
    assert "publish --push\n" in log


def test_release_end_to_end_times_out_unanswered_confirmation(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)

    returncode, output = run_with_confirmations(repository, environment, [])

    assert returncode != 0
    assert "confirmation timed out after 2s" in output
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "publish \n" in log
    assert "publish --push\n" not in log
    assert "pending_deployments" not in log


def test_release_end_to_end_rejects_preloaded_confirmations(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)

    terminal, slave = pty.openpty()
    os.write(terminal, b"v0.1.1\nv0.1.1\n")
    process = subprocess.Popen(
        [str(repository / "scripts" / "release-end-to-end"), "0.1.1"],
        cwd=repository,
        env=environment,
        stdin=slave,
        stdout=slave,
        stderr=slave,
        start_new_session=True,
    )
    os.close(slave)
    output = bytearray()
    deadline = time.monotonic() + 10
    try:
        while process.poll() is None and time.monotonic() < deadline:
            ready, _, _ = select.select([terminal], [], [], 0.1)
            if ready:
                try:
                    output.extend(os.read(terminal, 4096))
                except OSError:
                    break
        assert process.wait(timeout=1) != 0
    finally:
        os.close(terminal)

    assert "preloaded terminal input is forbidden" in output.decode(errors="replace")
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "gh pr create" not in log
    assert "gh pr merge" not in log
    assert "publish" not in log


def test_agent_release_driver_answers_only_observed_prompts(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)
    environment["TFR_RELEASE_DRIVER_TIMEOUT_SECONDS"] = "30"

    result = subprocess.run(
        [str(repository / "scripts" / "release-end-to-end-agent"), "0.1.1"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("Type v0.1.1") == 2
    assert "Release published and verified" in result.stdout
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "publish --push\n" in log
    assert (Path(environment["TFR_FAKE_STATE"]) / "approved").exists()


def test_agent_release_driver_handles_candidate_prompts(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)
    environment["TFR_FAKE_NO_CANDIDATE"] = "1"
    environment["TFR_RELEASE_DRIVER_TIMEOUT_SECONDS"] = "30"

    result = subprocess.run(
        [str(repository / "scripts" / "release-end-to-end-agent"), "0.1.1"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.count("Type v0.1.1-candidate.1") == 2
    assert "Test this candidate in the candidate channel" in result.stdout


def test_release_end_to_end_resumes_existing_version_only_pr(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)
    prepare_resume_fixture(repository, environment)

    returncode, output = run_with_confirmations(
        repository,
        environment,
        ["v0.1.1", "v0.1.1"],
        resume=True,
    )

    assert returncode == 0, output
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "gh pr create" not in log
    assert "--jq --arg" not in log
    assert "gh pr merge 2" in log
    assert "publish --push\n" in log
    assert "release: retrigger checks for v0.1.1" in git(
        repository, "log", "origin/release/v0.1.1", "-1", "--format=%s"
    )


def test_release_resume_rejects_unexpected_release_pr_files(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)
    prepare_resume_fixture(repository, environment)
    environment["TFR_FAKE_UNEXPECTED_RELEASE_FILE"] = "1"

    result = subprocess.run(
        [str(repository / "scripts" / "release-end-to-end"), "--resume", "0.1.1"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode != 0
    assert "release PR must change only pyproject.toml and uv.lock" in result.stderr
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "gh pr merge" not in log
    assert "publish" not in log


def test_release_resume_waits_through_transient_neutral_codeql_gate(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path)
    prepare_resume_fixture(repository, environment)
    environment["TFR_FAKE_TRANSIENT_NEUTRAL"] = "1"

    returncode, output = run_with_confirmations(
        repository,
        environment,
        ["v0.1.1", "v0.1.1"],
        resume=True,
    )

    assert returncode == 0, output
    assert int(
        (Path(environment["TFR_FAKE_STATE"]) / "check-run-calls").read_text()
    ) >= 2


def test_release_end_to_end_stops_on_security_failure(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path, security_failure=True)

    result = subprocess.run(
        [str(repository / "scripts" / "release-end-to-end"), "0.1.1"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 7
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "security\n" in log
    assert "gh pr create" not in log
    assert "gh pr merge" not in log
    assert "publish" not in log
    assert "pending_deployments" not in log


def test_release_end_to_end_stops_on_codeql_failure(tmp_path: Path) -> None:
    repository, environment = create_fixture(tmp_path, codeql_failure=True)

    result = subprocess.run(
        [str(repository / "scripts" / "release-end-to-end"), "0.1.1"],
        cwd=repository,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode != 0
    log = Path(environment["TFR_FAKE_LOG"]).read_text(encoding="utf-8")
    assert "/check-runs?per_page=100" in log
    assert "gh pr merge" not in log
    assert "publish" not in log
    assert "pending_deployments" not in log
