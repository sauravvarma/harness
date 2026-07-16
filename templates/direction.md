# Direction artifact: <project / feature>

Tag: content. Authored in charter mode, ratified by the operator, registered with
`harness direction <path>`. Dispatch of ui_surface tasks is blocked without this file.
The design-critic judges fidelity to THIS document, never its own preferences.

## Anchor reference
<One real design, described concretely: what it is, why it is the anchor, what
specifically to take from it (proportion, hierarchy, feel) and what NOT to take.
A named theme beats adjectives. Attach or link an image if possible.>

## Taste dials (label -> discipline)
- DESIGN_VARIANCE: <restrained (2-3) | confident (4-6) | distinctive (7-8)>
- MOTION_INTENSITY: <calm | responsive | expressive> <numeric bounds, e.g. UI transitions 150-300ms, one orchestrated entrance max>
- TYPE_CONTRAST: <quiet | editorial | display-led> <faces + scale steps>
- COLOR_ECONOMY: <strict (2 hues + neutrals) | warm (3) | rich (4+)>
- GRID_DENSITY: <airy | balanced | dense>
- PERSONALITY: <one sentence, concrete, no adjectives without referents>

## Banned patterns (stacks with the standing slop baseline)
- <project-specific bans, e.g. no gradient hero on white, no icon-tile card rows>
- Standing baseline always applies: generic gradient-on-white, three-equal-cards,
  01/02/03 markers, default-font-everywhere, bounce easing on UI, scale(0) entrances.

## Tokens (locked)
<Pointer to the token source of truth (file/theme). Builders may not introduce
colors, faces, radii, or spacing values outside it.>

## Feel clauses (compiled, no adjectives)
<Each a number, comparison, or ban. Examples of the required shape:
- pointer-driven audio: >= 700ms between triggers, intensity decays with velocity
- interruptible motion: any input mid-animation transitions within 100ms, no completion-waits
- text on art: text container derives from the art's geometry, never overlaid free-floating>

## Ratification
Operator: <name>  Date: <date>  (charter ratified via `harness charter ratify --by <operator>` when in genesis mode; otherwise this line is the record)
