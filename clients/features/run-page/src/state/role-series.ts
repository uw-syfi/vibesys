/**
 * The color of an agent role: one slot of the categorical palette (`--series-N` in `@vibesys/ui`)
 * per role, the same wherever the Run screen draws a role (the Agents list, the Timeline's bars and
 * its legend). Roles are open names, so a role no one preassigned takes a slot from a hash of its
 * name: it keeps its color from one render and one run to the next.
 */

/** How many slots the palette has. */
export const SERIES_SLOTS = 8;

/** Roles the searches use, each with a slot of its own so they never collide. */
const PREASSIGNED: readonly string[] = [
  'orchestrator',
  'implementer',
  'reviewer',
  'judge',
  'profiler',
  'mutator',
  'router',
  'planner',
];

function hashed(role: string): number {
  let hash = 2_166_136_261;
  for (const char of role) {
    hash = Math.imul(hash ^ (char.codePointAt(0) ?? 0), 16_777_619) >>> 0;
  }
  return hash % SERIES_SLOTS;
}

/** The palette slot of `role`, from 0 to `SERIES_SLOTS - 1`. */
export function roleSeries(role: string): number {
  const at = PREASSIGNED.indexOf(role);
  return at === -1 ? hashed(role) : at;
}
