import { createContractHooks } from "../lib/enforce-contract.js"

const EnforceContract = {
  id: "tfr.enforce-contract",
  async setup(ctx) {
    const hooks = createContractHooks()
    await ctx.session.hook("prompt", hooks.handlePrompt)
    await ctx.tool.hook("execute.before", hooks.handleTool)
  },
}

export default EnforceContract
