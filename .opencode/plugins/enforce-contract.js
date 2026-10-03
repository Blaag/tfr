const reviewCycles = new Map()
const releaseAuthorizations = new Set()

export function contentBearingGitDiff(command) {
  if (typeof command !== "string" || !/(^|\s)git\s+diff(?:\s|$)/.test(command)) {
    return false
  }
  return !/(^|\s)--(?:check|quiet|exit-code)(?:\s|$)/.test(command)
}

export function releaseAffectingCommand(command) {
  if (typeof command !== "string") {
    return false
  }
  return [
    /(?:^|[;&|]\s*)(?:\.\/)?scripts\/publish-release\s+--push(?:\s|$)/,
    /(?:^|[;&|]\s*)(?:\.\/)?scripts\/release-end-to-end(?:\s|$)/,
    /(?:^|[;&|]\s*)git(?:\s+-C\s+\S+)?\s+tag(?:\s|$)/,
    /(?:^|[;&|]\s*)git(?:\s+-C\s+\S+)?\s+push\b[^;&|\n]*(?:--tags|--follow-tags|refs\/tags\/)/,
    /(?:^|[;&|]\s*)gh\s+release\s+(?:create|delete|edit|upload)(?:\s|$)/,
    /(?:^|[;&|]\s*)gh\s+workflow\s+run\b[^;&|\n]*release/i,
    /(?:^|[;&|]\s*)gh\s+api\b[^;&|\n]*\breleases?\b[^;&|\n]*(?:--method\s+(?:POST|PATCH|PUT|DELETE)|-X\s*(?:POST|PATCH|PUT|DELETE))/i,
    /(?:^|[;&|]\s*)(?:uv|python\s+-m\s+twine|twine|npm|pnpm|yarn|cargo)\s+publish(?:\s|$)/,
  ].some((pattern) => pattern.test(command))
}

function securityCheck(command) {
  if (typeof command !== "string") {
    return null
  }
  if (/(^|\s)uv\s+run\s+bandit\b/.test(command)) {
    return "bandit"
  }
  if (/(^|\s)uv\s+run\s+pip-audit\b/.test(command)) {
    return "pipAudit"
  }
  return null
}

function testCheck(command) {
  return typeof command === "string" && /(^|\s)uv\s+run\s+pytest\s+-q(?:\s|$)/.test(command)
}

export function createContractHooks() {
  return {
    "command.execute.before": async (input) => {
      if (input.command === "review-start") {
        if (reviewCycles.has(input.sessionID)) {
          throw new Error("Run /review-end before starting another review cycle")
        }
        reviewCycles.set(input.sessionID, {
          diffConsumed: false,
          bandit: false,
          pipAudit: false,
          tests: false,
          investigations: 0,
        })
      } else if (input.command === "review-end") {
        const review = reviewCycles.get(input.sessionID)
        if (review === undefined) {
          throw new Error("No review cycle is active")
        }
        const missing = [
          !review.diffConsumed && "the content-bearing git diff",
          !review.tests && "the full test suite",
          !review.bandit && "Bandit",
          !review.pipAudit && "pip-audit",
        ].filter(Boolean)
        if (missing.length > 0) {
          throw new Error(`Review cannot end before running: ${missing.join(", ")}`)
        }
        reviewCycles.delete(input.sessionID)
      } else if (input.command === "release") {
        releaseAuthorizations.add(input.sessionID)
      }
    },
    "tool.execute.before": async (input, output) => {
      if (input.tool === "task") {
        throw new Error("Repository contract prohibits subagents and the task tool")
      }
      if (input.tool === "bash" && releaseAffectingCommand(output.args?.command)) {
        if (!releaseAuthorizations.delete(input.sessionID)) {
          throw new Error("Repository contract requires the user to run /release first")
        }
      }
      const check = input.tool === "bash" ? securityCheck(output.args?.command) : null
      if (check !== null && reviewCycles.has(input.sessionID)) {
        reviewCycles.get(input.sessionID)[check] = true
      }
      if (
        input.tool === "bash" &&
        testCheck(output.args?.command) &&
        reviewCycles.has(input.sessionID)
      ) {
        reviewCycles.get(input.sessionID).tests = true
      }
      const activeReview = reviewCycles.get(input.sessionID)
      if (activeReview !== undefined && ["read", "grep", "glob"].includes(input.tool)) {
        if (!activeReview.diffConsumed) {
          throw new Error("Run automated checks and inspect the diff before manual investigation")
        }
        if (input.tool === "glob") {
          throw new Error("Broad file exploration is prohibited during a review")
        }
        if (activeReview.investigations >= 6) {
          throw new Error("Review reached its six targeted-investigation limit")
        }
        activeReview.investigations += 1
      }
      if (input.tool !== "bash" || !contentBearingGitDiff(output.args?.command)) {
        return
      }
      if (!reviewCycles.has(input.sessionID)) {
        throw new Error("Run /review-start before inspecting a content-bearing git diff")
      }
      const review = reviewCycles.get(input.sessionID)
      const missingChecks = [
        !review.tests && "the full test suite",
        !review.bandit && "Bandit",
        !review.pipAudit && "pip-audit",
      ].filter(Boolean)
      if (missingChecks.length > 0) {
        throw new Error(`Run automated checks before inspecting the diff: ${missingChecks.join(", ")}`)
      }
      if (review.diffConsumed) {
        throw new Error("This review cycle already consumed its one content-bearing git diff")
      }
      review.diffConsumed = true
    },
  }
}

export const EnforceContract = async () => createContractHooks()
