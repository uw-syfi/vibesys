/**
 * The agent kind of an experiment-chat session. A chat session runs beside a run, not as one of
 * its roles: it spends and streams on the run, but no flow declares it and no steer addresses it.
 * Every client that tells a chat session from a role of the run asks `isChatAgentKind`, so the
 * word is written once.
 */
export const CHAT_AGENT_KIND = 'chat';

/** Whether an event's `agent_kind`, or a usage record's `role`, names a chat session. */
export function isChatAgentKind(kind: string | null | undefined): boolean {
  return kind === CHAT_AGENT_KIND;
}
