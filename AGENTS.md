# OpenCode Contract

These rules are mandatory for every OpenCode conversation in this repository.

## No Subagents

- Never invoke the `task` tool or any subagent, including for exploration, review, security review, or Terraform work.
- Use direct repository tools and deterministic commands instead.

## Diff Review

- Start each review cycle explicitly with `/review-start` and end it with `/review-end`.
- Run the full test suite and required security tools before reading code or inspecting the diff.
- Inspect the working-tree diff once per review cycle with one content-bearing `git diff` command.
- Before that inspection, `git diff --check`, `git diff --quiet`, and `git diff --exit-code` are allowed because they do not expose diff content.
- Content-bearing `git diff` commands are prohibited outside a review cycle.
- After inspecting the diff, do not run another content-bearing `git diff` until a new `/review-start` opens a new cycle.
- Report no more than five findings and no fewer than three only when three validated issues actually exist. Never invent findings to meet a quota.
- A finding is valid only when supported by the inspected diff and current repository evidence.
- Treat passing tests as sufficient for behavior they directly cover; do not manually re-prove it.
- Manually investigate only a failing check or a concrete uncovered changed branch visible in the diff.
- State the concrete hypothesis and relevant existing coverage before each targeted read or search.
- Stop when existing coverage resolves the hypothesis. Prefer a regression test over prolonged tracing when coverage is unclear.
- Broad file exploration is prohibited during review. At most six targeted reads/searches and three proposed tests are allowed per cycle.
- Do not revisit a rejected hypothesis unless new test output or other new evidence directly supports it.
- Prefer targeted automated tests for validated edge cases. Do not add speculative tests for unvalidated hypotheses.

## Verification

- Use repository-native linters, tests, type checks, and `git diff --check` directly.
- During `/review-start`, run `uv run pytest -q`, `uv run bandit -q -r src`, and `uv run pip-audit`; the review gate rejects diff inspection until all three are invoked and rejects `/review-end` until they and the diff inspection have occurred. Report failures rather than claiming they passed.
- Do not substitute model-based review for deterministic verification.

## Release Gate

- Never create or publish a release unless the user explicitly invokes `/release`.
- `/release` authorizes exactly one release-affecting command in that session and is consumed even when the command fails.
- Use `./scripts/publish-release --push` as the canonical release path. Do not bypass or weaken the release gate.
