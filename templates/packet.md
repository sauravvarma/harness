# Packet <TASK-ID>: <short name>

Tag: content. Written by the orchestrator; the compressed form of its judgment.
Target: ~40 lines. If it will not fit, the decomposition is wrong, not the cap.

## Goal
<one paragraph: what exists when this packet is done>

## Contracts
<ids only; full text lives in the plan. e.g. C1 (v1), C3 (v1)>

## Scope
- Allowed: <files/dirs this packet may touch>
- Forbidden: <files/dirs it must not touch>

## Constraints
- <standards doc section refs, dependency rules, style/idiom notes>
- Map pointers: <.harness/map.md sections relevant here>

## Acceptance (executable; the CLI records these, claims do not count)
- `<command 1>`
- `<command 2>`

## Budget
- turns: <n>

## Escalate if
- <the specific discovery that should trigger contract-conflict>
- <the specific tradeoff that should trigger needs-decision>
