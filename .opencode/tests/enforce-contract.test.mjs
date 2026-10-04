import assert from "node:assert/strict"
import { readFile } from "node:fs/promises"
import test from "node:test"

import {
  contentBearingGitDiff,
  createContractHooks,
  releaseAffectingCommand,
} from "../lib/enforce-contract.js"
import * as plugin from "../plugins/enforce-contract.js"

const shell = (sessionID, command) => ({ tool: "shell", sessionID, input: { command } })
const command = (sessionID, name) => ({
  sessionID,
  prompt: { text: `<!-- tfr-command:${name} -->\n\nRun ${name}.` },
})

const runReviewChecks = (hooks, sessionID) => {
  hooks.handleTool(shell(sessionID, "uv run pytest -q"))
  hooks.handleTool(shell(sessionID, "uv run bandit -q -r src"))
  hooks.handleTool(shell(sessionID, "uv run pip-audit"))
}

test("auto-loaded plugin module exports exactly one plugin", () => {
  assert.deepEqual(Object.keys(plugin), ["default"])
  assert.equal(plugin.default.id, "tfr.enforce-contract")
})

test("V2 project instructions mirror the canonical contract", async () => {
  const agents = await readFile(new URL("../../AGENTS.md", import.meta.url), "utf8")
  const contract = await readFile(new URL("../../OPENCODE-CONTRACT.md", import.meta.url), "utf8")
  assert.equal(agents, contract)
})

test("identifies only content-bearing git diff commands", () => {
  assert.equal(contentBearingGitDiff("git diff -- src/app.py"), true)
  assert.equal(contentBearingGitDiff("git diff --staged"), true)
  assert.equal(contentBearingGitDiff("git diff --check"), false)
  assert.equal(contentBearingGitDiff("git diff --quiet"), false)
  assert.equal(contentBearingGitDiff("git status --short"), false)
})

test("recognizes command markers and removes them before admission", () => {
  const hooks = createContractHooks()
  const event = command("marker", "release")
  hooks.handlePrompt(event)
  assert.equal(event.prompt.text, "Run release.")
})

test("rejects every subagent invocation", () => {
  const hooks = createContractHooks()
  assert.throws(
    () => hooks.handleTool({ tool: "task", sessionID: "subagent", input: {} }),
    /prohibits subagents/,
  )
  assert.throws(
    () => hooks.handleTool({ tool: "subagent", sessionID: "subagent", input: {} }),
    /prohibits subagents/,
  )
})

test("allows exactly one content-bearing diff per explicit review cycle", () => {
  const hooks = createContractHooks()
  const sessionID = "bounded-review"

  assert.throws(() => hooks.handleTool(shell(sessionID, "git diff")), /review-start/)
  hooks.handlePrompt(command(sessionID, "review-start"))
  runReviewChecks(hooks, sessionID)
  hooks.handleTool(shell(sessionID, "git diff -- src"))
  assert.throws(() => hooks.handleTool(shell(sessionID, "git diff -- tests")), /already consumed/)
  assert.throws(() => hooks.handlePrompt(command(sessionID, "review-start")), /review-end/)
  hooks.handlePrompt(command(sessionID, "review-end"))
  assert.throws(() => hooks.handleTool(shell(sessionID, "git diff")), /review-start/)
})

test("requires security tooling before a review can end", () => {
  const hooks = createContractHooks()
  const sessionID = "security-review"

  hooks.handlePrompt(command(sessionID, "review-start"))
  assert.throws(
    () => hooks.handleTool(shell(sessionID, "git diff")),
    /full test suite, Bandit, pip-audit/,
  )
  hooks.handleTool(shell(sessionID, "uv run pytest -q"))
  assert.throws(
    () => hooks.handlePrompt(command(sessionID, "review-end")),
    /content-bearing git diff, Bandit, pip-audit/,
  )
  hooks.handleTool(shell(sessionID, "uv run bandit -q -r src"))
  assert.throws(() => hooks.handleTool(shell(sessionID, "git diff")), /pip-audit/)
  hooks.handleTool(shell(sessionID, "uv run pip-audit"))
  hooks.handleTool(shell(sessionID, "git diff"))
  hooks.handlePrompt(command(sessionID, "review-end"))
})

test("bounds manual review investigation after automated checks", () => {
  const hooks = createContractHooks()
  const sessionID = "bounded-investigation"

  hooks.handlePrompt(command(sessionID, "review-start"))
  assert.throws(
    () => hooks.handleTool({ tool: "read", sessionID, input: { filePath: "src/app.py" } }),
    /automated checks and inspect the diff/,
  )
  runReviewChecks(hooks, sessionID)
  hooks.handleTool(shell(sessionID, "git diff"))
  assert.throws(
    () => hooks.handleTool({ tool: "glob", sessionID, input: { pattern: "**/*" } }),
    /Broad file exploration/,
  )
  for (let index = 0; index < 6; index += 1) {
    hooks.handleTool({
      tool: "grep",
      sessionID,
      input: { pattern: `candidate-${index}`, path: "src" },
    })
  }
  assert.throws(
    () => hooks.handleTool({ tool: "read", sessionID, input: { filePath: "src/app.py" } }),
    /six targeted-investigation limit/,
  )
})

test("allows non-content diff checks outside review cycles", () => {
  const hooks = createContractHooks()
  hooks.handleTool(shell("checks", "git diff --check"))
  hooks.handleTool(shell("checks", "git diff --quiet"))
})

test("identifies release-affecting commands", () => {
  assert.equal(releaseAffectingCommand("./scripts/publish-release"), false)
  assert.equal(releaseAffectingCommand("./scripts/publish-release --push"), true)
  assert.equal(releaseAffectingCommand("./scripts/release-end-to-end 1.2.3"), true)
  assert.equal(releaseAffectingCommand("git status --short -- scripts/release-end-to-end"), false)
  assert.equal(releaseAffectingCommand("git add -- scripts/publish-release"), false)
  assert.equal(releaseAffectingCommand("git tag -a v1.2.3 -m release"), true)
  assert.equal(releaseAffectingCommand("git push origin refs/tags/v1.2.3"), true)
  assert.equal(releaseAffectingCommand("gh release create v1.2.3"), true)
  assert.equal(releaseAffectingCommand("uv publish"), true)
  assert.equal(releaseAffectingCommand("uv run pytest"), false)
})

test("requires one explicit authorization per release command", () => {
  const hooks = createContractHooks()
  const sessionID = "release-gate"
  const release = shell(sessionID, "./scripts/publish-release --push")

  assert.throws(() => hooks.handleTool(release), /run \/release first/)
  hooks.handlePrompt(command(sessionID, "release"))
  hooks.handleTool(release)
  assert.throws(() => hooks.handleTool(release), /run \/release first/)
})
