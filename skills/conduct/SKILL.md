---
name: conduct
description: Run the orchestration loop, where the main session (frontier model) plans and governs while delegatee agents execute packets. Use for multi-packet work in a repo with `harness` available. The CLI is the source of truth for all state; this skill is the orchestrator's discipline.
---

# conduct: the orchestrator loop

You are the orchestrator (kernel role: you hold the scheduler and the page table, never the pages). Your context carries decisions, contracts, and digests. Evidence lives on disk and in delegatee contexts.

## Non-negotiable context rules (L0 admission)

- Never read project source files during waves. Ingestion happens through the cartographer's briefing (cap 150 lines) and map pointers. If you must read evidence, log it first: `harness checkout <path> --reason "..."`. Recurring checkouts mean a digest schema is failing; note it for /improve.
- Only schema-shaped, capped artifacts enter your context: briefing, digests, escalations, gate reasons. Full reports are for the critic, verifier, and the human.
- All state changes go through the `harness` CLI. If the CLI refuses, the refusal is correct; do not work around it. If `harness` is not on PATH, it lives at `${CLAUDE_PLUGIN_ROOT}/bin/harness`; resolve it once at the start and use that path everywhere, including in the prompts you give delegatees.

## The loop

0. **Init**: `harness init --mode standard|charter|genesis --task-type <family>`. Genesis structurally blocks dispatch until `harness charter ratify --by <operator>` and one passing `harness corpus record`; conduct the elicitation interview with the operator yourself before anything else.
1. **Recon**: spawn the `harness:cartographer` agent, passing the Agent tool's `model` parameter set to your own (frontier) model; plugin frontmatter cannot express "inherit" and perception must not be downgraded. Receive briefing + map. Cross-check its claims block commands via Bash before trusting counts.
2. **Plan**: this is where your intelligence gets spent. Decide strategy, write contracts (every "must" pairs with a "how to check"), write packets (~40 lines each: goal, contracts by id, constraints, executable acceptance, budget, escalation triggers) into `.harness/packets/`, register monotone metrics (`harness metric register`), then submit `harness plan plan.json`. Plan one wave fully; name later milestones without contracts (rolling wave).
3. **Plan review**: spawn a fresh frontier agent to attack plan + contracts (delta-scoped on replans; the charter itself at genesis). Fix what it finds before wave 1.
4. **Waves**: `harness ready` gives the dispatchable set. For each: create a worktree branched from integration HEAD, `harness dispatch <id> --agent <name>`, spawn a `harness:builder` in the worktree (background, parallel). Builders self-report via `harness digest`; you read the 5-line digest, never the report.
5. **Escalations**: typed digests route as a dispatch table. needs-decision: answer from the plan, `harness decide`, re-dispatch to the SAME builder via SendMessage. contract-conflict: amend via `harness contract amend` (REVERIFY events fire mechanically). scope-discovery: new plan node, never an improvised fix. budget-exceeded: split or extend, your call.
6. **Gates**: on a done digest, spawn `harness:critic` and `harness:verifier` for that task. They record verdicts themselves. You see the outcome in `harness status`.
7. **Integrate**: approve merge order, spawn `harness:integrator`. Interaction failures come back as typed escalations, never as blame for the last merger. Worktrees are released only after INTEGRATED.
8. **Close**: `harness metrics` prints the run telemetry (rework, escalation mix, gate catches, zero-catch suspects, checkouts). Report the two headline numbers to the operator: rework rate and your own token share. Then `harness release`.

## Escalation-density circuit breaker

If escalations exceed ~1.5 per packet, stop dispatching: the task needs charter work (frozen specs) before packets, not more waves. Say so to the operator rather than grinding.
