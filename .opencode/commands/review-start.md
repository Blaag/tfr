---
description: Start a bounded diff review cycle
agent: build
---

Immediately execute this test-first review cycle; do not merely describe it or wait for another
message. Before reading code or inspecting the diff, run `uv run pytest -q`,
`uv run bandit -q -r src`, and `uv run pip-audit`. Report failures honestly. Then inspect the
working-tree diff exactly once with one content-bearing `git diff` command.

Treat passing tests as sufficient verification for behavior they directly cover. Investigate
manually only when a check fails or the diff supports a concrete hypothesis about an uncovered
changed branch. Before each targeted read or search, state the hypothesis and the existing test
coverage already considered. Stop investigating when an existing test resolves the hypothesis.
Prefer adding a regression test over prolonged tracing when coverage is unclear. Do not use broad
file exploration. Use no more than six targeted reads/searches, propose no more than three tests,
and report at most five validated findings. Never invent findings. Do not use subagents. Run
`/review-end` when the review is complete.
