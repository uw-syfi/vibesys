/**
 * How many agents of each type are working across the runs a host has summarised: the sum of the
 * `working` counts the host served in each run's summary (the host decides what working means). The
 * summaries are the runs the client holds, and a run that is working is always one of them, so this
 * is every agent working now even when the host's cost view counts older runs the client holds no
 * summary of.
 */
import type {AgentRole} from '@vibesys/orchestrator-proto';

export function workingByRole(
  summaries: Readonly<
    Record<
      string,
      {readonly roles: readonly {readonly role: AgentRole; readonly working: number}[]}
    >
  >,
): ReadonlyMap<AgentRole, number> {
  const working = new Map<AgentRole, number>();
  for (const summary of Object.values(summaries)) {
    for (const {role, working: count} of summary.roles) {
      working.set(role, (working.get(role) ?? 0) + count);
    }
  }
  return working;
}
