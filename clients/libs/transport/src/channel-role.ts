/**
 * The semantic purpose of one run-protocol connection.
 *
 * A transport opens a separate connection for each role so connection owners can reserve capacity
 * for operator control without inspecting protocol frames. Keep this set closed at every boundary:
 * adding a role requires an explicit admission and host-helper mapping decision.
 */
export const RUN_CHANNEL_ROLES = ['run-control', 'subscribe', 'chat'] as const;

export type RunChannelRole = (typeof RUN_CHANNEL_ROLES)[number];

/** Parse an untrusted bridge value into the closed role set. */
export function parseRunChannelRole(value: unknown): RunChannelRole | null {
  return RUN_CHANNEL_ROLES.find(role => role === value) ?? null;
}
