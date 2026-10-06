# Maintainer Guide

This guide covers stable TFR releases and the repository scripts involved in
building, publishing, and installing them.

## Script Reference

| Command | Intended user | Purpose |
| --- | --- | --- |
| `./scripts/release-end-to-end VERSION` | Maintainer | Run local CI/security checks, merge the current work and version bump through protected PRs, require passing CI/Dependency Review/CodeQL, push the stable tag, approve protected publication, and verify the immutable release. |
| `./scripts/release-end-to-end-agent VERSION` | OpenCode release agent | Run the same protected end-to-end release while answering each exact-tag confirmation only after its prompt is observed. Requires prior `/release` authorization. |
| `./scripts/publish-release` | Maintainer | Validate that the current `main` commit is ready for the version in `pyproject.toml`. It makes no release change without `--push`. |
| `./scripts/publish-release --push` | Maintainer | Create and push the matching annotated `vX.Y.Z` tag, triggering the protected release workflow. |
| `./scripts/install-from-checkout` | Operator or developer | Build the current clean checkout into an isolated local release and optionally activate it. It does not publish anything. |
| `./scripts/install-from-checkout --latest-stable` | Operator | Fetch the live official manifest, verify its exact annotated tag and commit, then build and activate that tagged source. It never installs `main`. |
| `scripts/build_update_manifest.py` | GitHub Actions | Generate `update-manifest.json` for an already-built wheel. It is a release-workflow helper and is not normally run manually. |

The actual package build and GitHub Release publication are performed by
`.github/workflows/release.yml`. Keeping publication in GitHub Actions separates
the unprivileged build job from the protected job with `contents: write`.

## Stable Release Process

### Automated end-to-end release

For the normal path, start from a clean feature branch containing the release's
changes, or from a clean `main` when those changes have already been merged:

```console
./scripts/release-end-to-end 0.1.24
```

The script is pinned to the official `Blaag/tfr` repository and fails closed. It:

1. Verifies the active main-branch and stable-tag rulesets, release-environment
   reviewer, immutable-release setting, GitHub authentication, lock file, and clean
   checkout.
2. Runs the same security, lint, web, end-to-end, Python, and package-build checks as
   CI.
3. Pushes the current feature branch, creates or reuses its PR, requires the complete
   CI, Dependency Review, and CodeQL check set, and squash-merges without bypassing
   repository rules.
4. Creates `release/vVERSION`, updates `pyproject.toml` and `uv.lock`, reruns all local
   checks, commits the version, and merges its protected PR after the same checks pass.
5. Waits for CI and CodeQL to pass on the exact merged `main` commit.
6. Previews `./scripts/publish-release`, requires you to type the exact tag, and pushes
   the immutable tag.
7. Waits for the release build to pass, requires you to type the exact tag again,
   approves the protected `release` environment through GitHub, and waits for
   publication.
8. Verifies that the GitHub Release is immutable and contains the wheel, source
   distribution, and `update-manifest.json`.

The script never uses `--admin`, force-pushes, deletes tags, dismisses failures, or
approves publication before the protected build passes. Any command or check failure
stops execution. If OpenCode runs the command, invoke `/release` first; running it
directly in your shell does not use the OpenCode gate.

Run the script in an interactive terminal and enter each exact-tag confirmation only
when its prompt appears. Do not preload confirmations through a pipe or pseudo-terminal:
the script detects queued terminal input before release work and fails closed. Earlier
interactive `gh ... --watch` commands may otherwise consume buffered input. Each prompt
expires after five minutes and fails closed instead of waiting indefinitely. Maintainers
can shorten that bound for supervised automation with the positive-integer
`TFR_RELEASE_CONFIRM_TIMEOUT_SECONDS` environment variable; it does not remove or
automatically answer either confirmation.

After explicit `/release` authorization, OpenCode must invoke
`./scripts/release-end-to-end-agent VERSION` directly instead of constructing a PTY or
piping confirmations. The driver mirrors output, waits for each exact version-specific
prompt, responds once in the required order, enforces its own one-hour overall timeout,
and requires the orchestrator's final immutable-release verification before succeeding.

If a guarded release created a version-only release PR but stopped before merging it,
resume from clean, synchronized `main` with
`./scripts/release-end-to-end-agent --resume VERSION`. Resume mode requires the exact
remote release branch and one open non-draft PR, rejects changes outside
`pyproject.toml` and `uv.lock`, rejects an existing tag or Release, merges current
protected `main` into the release branch, reruns local checks, and pushes a new commit
to trigger a complete fresh protected check set before continuing through the same
canonical publish and approval path.

After the stable tag is pushed, rerun the script only after understanding the failure.
The tag and release are intentionally immutable; use the failure-recovery guidance
below rather than deleting or replacing them.

### Manual release

Before publishing, update `project.version` in `pyproject.toml`, refresh
`uv.lock`, commit the version change, and push `main`. Versions must be stable
semantic versions such as `0.1.0` or `0.1.1`; prerelease versions are not part
of the stable update channel.

Preview the release first:

```console
./scripts/publish-release
```

The preview requires all of the following:

- The checkout is on `main`.
- There are no modified, staged, or untracked files.
- No merge, rebase, cherry-pick, revert, or bisect is in progress.
- `origin` has one fetch URL and one identical push URL.
- The local `HEAD` exactly matches the freshly fetched `origin/main`.
- `pyproject.toml` names the `tfr` project and contains a stable version.
- The corresponding annotated local tag is absent, or is a safe retry tag that
  points to `HEAD`.
- The corresponding remote tag is absent, or exactly matches the verified local
  retry tag.
- The version is newer than every stable semantic-version tag on the remote.

The preview does not create or push a tag. When its output identifies the
expected version and commit, publish the release trigger explicitly:

```console
./scripts/publish-release --push
```

The command rechecks the checkout and remote immediately before creating an
annotated `vX.Y.Z` tag. It pushes only that tag; it never pushes branch commits,
pulls, merges, stashes, replaces tags, or calls `gh release create` locally.

## GitHub Release Workflow

Pushing the tag starts `.github/workflows/release.yml`, which:

1. Verifies that the tagged commit is on `main`.
2. Installs the frozen lock, runs Ruff, and runs the release test suite, excluding
   the documented hanging isolation test.
3. Embeds the exact tagged commit in the package.
4. Builds the wheel and source distribution.
5. Generates `update-manifest.json` with the wheel URL, size, SHA-256 digest,
   and supported Gateway protocol range.
6. Transfers those files to the protected `release` environment.
7. Serializes protected publication and verifies that the tag still identifies
   the workflow commit, remains on `main`, and is the newest stable version.
8. Publishes an immutable GitHub Release containing all assets.

If the `release` environment requires approval, approve the publish job in the
GitHub Actions interface. Repository rules should protect stable release tags
and enable immutable releases.

Inspect the result with:

```console
gh run list --workflow Release --limit 5
gh release view v0.1.0
```

After the first release, this URL must return the published manifest rather
than HTTP 404:

```text
https://github.com/Blaag/tfr/releases/latest/download/update-manifest.json
```

Running `/update check` in TFR checks that manifest without installing it. Bare
`/update` explicitly stages the verified tagged source on the Gateway and all
connected managed native UIs, then activates and restarts them after all are
ready. Protocol-changing releases still require manual coordinated deployment.

To install the published source release explicitly, run:

```console
./scripts/install-from-checkout --latest-stable
```

The stable installer fetches a fresh manifest without falling back to the
notification cache. It fetches only `refs/tags/vX.Y.Z` from the fixed official
repository, requires an annotated tag that points directly to the manifest's
commit, verifies the tagged `pyproject.toml` version, and then uses the tagged
`uv.lock` and existing atomic managed installer. The bootstrap checkout and
`main` are not changed or installed. Normal stable installation refuses a
downgrade or reuse of an installed stable version for a different commit;
rollback remains a separate explicit operation.

## Failure Recovery

If tag creation succeeds but the push fails, the script retains the verified
annotated local tag. Fix the network or credentials and rerun
`./scripts/publish-release --push`; it accepts that local tag only when it still
points to the validated `HEAD`.

If the remote tag already contains the exact verified local tag object, rerunning
the command reports success without changing it. Otherwise, do not delete,
replace, or force-push the remote tag. Inspect the GitHub Actions run and retry
the failed workflow job after correcting the environment or repository
configuration. A remote stable tag is treated as immutable.

If local and remote `main` differ, the script stops without changing either.
Review the difference, update or push `main` deliberately, and rerun the
preview.

## Checkout Installation

`./scripts/install-from-checkout` is unrelated to publishing. It packages a
clean local `HEAD` as `VERSION+git.COMMIT`, installs it under
`~/.local/share/tfr/releases`, and points `~/.local/bin/tfr` at the activated
release. It does not alter Git history, tags, GitHub Releases, configuration,
logs, plugin source state, or an already running TFR process.

See the versioned installation section in [README.md](README.md) for adoption,
activation, and rollback instructions.
