const reviewCycles = new Map()
const releaseAuthorizations = new Set()
const commandMarker = /^\s*<!-- tfr-command:(review-start|review-end|release) -->\s*/

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

function runCommand(command, sessionID) {
  if (command === "review-start") {
    if (reviewCycles.has(sessionID)) {
      throw new Error("Run /review-end before starting another review cycle")
    }
    reviewCycles.set(sessionID, {
      diffConsumed: false,
      bandit: false,
      pipAudit: false,
      tests: false,
      investigations: 0,
    })
  } else if (command === "review-end") {
    const review = reviewCycles.get(sessionID)
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
    reviewCycles.delete(sessionID)
  } else if (command === "release") {
    releaseAuthorizations.add(sessionID)
  }
}

export function createContractHooks() {
  return {
    handlePrompt(event) {
      const match = event.prompt.text.match(commandMarker)
      if (match === null) {
        return
      }
      runCommand(match[1], event.sessionID)
      event.prompt.text = event.prompt.text.replace(commandMarker, "")
    },

    handleTool(event) {
      if (["task", "subagent"].includes(event.tool)) {
        throw new Error("Repository contract prohibits subagents and the task tool")
      }

      const shell = ["bash", "shell"].includes(event.tool)
      const command = shell ? event.input?.command : undefined
      if (shell && releaseAffectingCommand(command)) {
        if (!releaseAuthorizations.delete(event.sessionID)) {
          throw new Error("Repository contract requires the user to run /release first")
        }
      }

      const check = shell ? securityCheck(command) : null
      if (check !== null && reviewCycles.has(event.sessionID)) {
        reviewCycles.get(event.sessionID)[check] = true
      }
      if (shell && testCheck(command) && reviewCycles.has(event.sessionID)) {
        reviewCycles.get(event.sessionID).tests = true
      }

      const activeReview = reviewCycles.get(event.sessionID)
      if (activeReview !== undefined && ["read", "grep", "glob"].includes(event.tool)) {
        if (!activeReview.diffConsumed) {
          throw new Error("Run automated checks and inspect the diff before manual investigation")
        }
        if (event.tool === "glob") {
          throw new Error("Broad file exploration is prohibited during a review")
        }
        if (activeReview.investigations >= 6) {
          throw new Error("Review reached its six targeted-investigation limit")
        }
        activeReview.investigations += 1
      }

      if (!shell || !contentBearingGitDiff(command)) {
        return
      }
      if (!reviewCycles.has(event.sessionID)) {
        throw new Error("Run /review-start before inspecting a content-bearing git diff")
      }
      const review = reviewCycles.get(event.sessionID)
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
