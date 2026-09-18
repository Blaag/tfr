# Maintainer Guide

This guide covers stable TFR releases and the repository scripts involved in
building, publishing, and installing them.

## Script Reference

| Command | Intended user | Purpose |
| --- | --- | --- |
| `./scripts/publish-release` | Maintainer | Validate that the current `main` commit is ready for the version in `pyproject.toml`. It makes no release change without `--push`. |
| `./scripts/publish-release --push` | Maintainer | Create and push the matching annotated `vX.Y.Z` tag, triggering the protected release workflow. |
| `./scripts/install-from-checkout` | Operator or developer | Build the current clean checkout into an isolated local release and optionally activate it. It does not publish anything. |
| `scripts/build_update_manifest.py` | GitHub Actions | Generate `update-manifest.json` for an already-built wheel. It is a release-workflow helper and is not normally run manually. |

The actual package build and GitHub Release publication are performed by
`.github/workflows/release.yml`. Keeping publication in GitHub Actions separates
the unprivileged build job from the protected job with `contents: write`.

## Stable Release Process

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

Running `/update check` in TFR then checks that manifest. Update checks are
notification-only and do not install the advertised artifact.

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
