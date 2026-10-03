import assert from "node:assert/strict"
import test from "node:test"

import {
  contentBearingGitDiff,
  createContractHooks,
  releaseAffectingCommand,
} from "../plugins/enforce-contract.js"

const bash = (sessionID, command) => [
  { tool: "bash", sessionID, callID: "call" },
  { args: { command } },
]

const runReviewChecks = async (hooks, sessionID) => {
  await hooks["tool.execute.before"](...bash(sessionID, "uv run pytest -q"))
  await hooks["tool.execute.before"](...bash(sessionID, "uv run bandit -q -r src"))
  await hooks["tool.execute.before"](...bash(sessionID, "uv run pip-audit"))
}

test("identifies only content-bearing git diff commands", () => {
  assert.equal(contentBearingGitDiff("git diff -- src/app.py"), true)
  assert.equal(contentBearingGitDiff("git diff --staged"), true)
  assert.equal(contentBearingGitDiff("git diff --check"), false)
  assert.equal(contentBearingGitDiff("git diff --quiet"), false)
  assert.equal(contentBearingGitDiff("git status --short"), false)
})

test("rejects every subagent invocation", async () => {
  const hooks = createContractHooks()
  await assert.rejects(
    hooks["tool.execute.before"](
      { tool: "task", sessionID: "subagent", callID: "call" },
      { args: {} },
    ),
    /prohibits subagents/,
  )
})

test("allows exactly one content-bearing diff per explicit review cycle", async () => {
  const hooks = createContractHooks()
  const sessionID = "bounded-review"

  await assert.rejects(
    hooks["tool.execute.before"](...bash(sessionID, "git diff")),
    /review-start/,
  )
  await hooks["command.execute.before"]({ command: "review-start", sessionID }, {})
  await runReviewChecks(hooks, sessionID)
  await hooks["tool.execute.before"](...bash(sessionID, "git diff -- src"))
  await assert.rejects(
    hooks["tool.execute.before"](...bash(sessionID, "git diff -- tests")),
    /already consumed/,
  )
  await assert.rejects(
    hooks["command.execute.before"]({ command: "review-start", sessionID }, {}),
    /review-end/,
  )
  await hooks["command.execute.before"]({ command: "review-end", sessionID }, {})
  await assert.rejects(
    hooks["tool.execute.before"](...bash(sessionID, "git diff")),
    /review-start/,
  )
})

test("requires security tooling before a review can end", async () => {
  const hooks = createContractHooks()
  const sessionID = "security-review"

  await hooks["command.execute.before"]({ command: "review-start", sessionID }, {})
  await assert.rejects(
    hooks["tool.execute.before"](...bash(sessionID, "git diff")),
    /full test suite, Bandit, pip-audit/,
  )
  await hooks["tool.execute.before"](...bash(sessionID, "uv run pytest -q"))
  await assert.rejects(
    hooks["command.execute.before"]({ command: "review-end", sessionID }, {}),
    /content-bearing git diff, Bandit, pip-audit/,
  )
  await hooks["tool.execute.before"](...bash(sessionID, "uv run bandit -q -r src"))
  await assert.rejects(
    hooks["tool.execute.before"](...bash(sessionID, "git diff")),
    /pip-audit/,
  )
  await hooks["tool.execute.before"](...bash(sessionID, "uv run pip-audit"))
  await hooks["tool.execute.before"](...bash(sessionID, "git diff"))
  await hooks["command.execute.before"]({ command: "review-end", sessionID }, {})
})

test("bounds manual review investigation after automated checks", async () => {
  const hooks = createContractHooks()
  const sessionID = "bounded-investigation"

  await hooks["command.execute.before"]({ command: "review-start", sessionID }, {})
  await assert.rejects(
    hooks["tool.execute.before"](
      { tool: "read", sessionID, callID: "call" },
      { args: { filePath: "src/app.py" } },
    ),
    /automated checks and inspect the diff/,
  )
  await runReviewChecks(hooks, sessionID)
  await hooks["tool.execute.before"](...bash(sessionID, "git diff"))
  await assert.rejects(
    hooks["tool.execute.before"](
      { tool: "glob", sessionID, callID: "call" },
      { args: { pattern: "**/*" } },
    ),
    /Broad file exploration/,
  )
  for (let index = 0; index < 6; index += 1) {
    await hooks["tool.execute.before"](
      { tool: "grep", sessionID, callID: `call-${index}` },
      { args: { pattern: `candidate-${index}`, path: "src" } },
    )
  }
  await assert.rejects(
    hooks["tool.execute.before"](
      { tool: "read", sessionID, callID: "call-limit" },
      { args: { filePath: "src/app.py" } },
    ),
    /six targeted-investigation limit/,
  )
})

test("allows non-content diff checks outside review cycles", async () => {
  const hooks = createContractHooks()
  await hooks["tool.execute.before"](...bash("checks", "git diff --check"))
  await hooks["tool.execute.before"](...bash("checks", "git diff --quiet"))
})

test("identifies release-affecting commands", () => {
  assert.equal(releaseAffectingCommand("./scripts/publish-release"), false)
  assert.equal(releaseAffectingCommand("./scripts/publish-release --push"), true)
  assert.equal(releaseAffectingCommand("./scripts/release-end-to-end 1.2.3"), true)
  assert.equal(
    releaseAffectingCommand("git status --short -- scripts/release-end-to-end"),
    false,
  )
  assert.equal(
    releaseAffectingCommand("git add -- scripts/publish-release"),
    false,
  )
  assert.equal(releaseAffectingCommand("git tag -a v1.2.3 -m release"), true)
  assert.equal(releaseAffectingCommand("git push origin refs/tags/v1.2.3"), true)
  assert.equal(releaseAffectingCommand("gh release create v1.2.3"), true)
  assert.equal(releaseAffectingCommand("uv publish"), true)
  assert.equal(releaseAffectingCommand("uv run pytest"), false)
})

test("requires one explicit authorization per release command", async () => {
  const hooks = createContractHooks()
  const sessionID = "release-gate"
  const release = bash(sessionID, "./scripts/publish-release --push")

  await assert.rejects(hooks["tool.execute.before"](...release), /run \/release first/)
  await hooks["command.execute.before"]({ command: "release", sessionID }, {})
  await hooks["tool.execute.before"](...release)
  await assert.rejects(hooks["tool.execute.before"](...release), /run \/release first/)
})
