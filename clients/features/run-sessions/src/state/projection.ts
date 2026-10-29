import type {CoreState} from '@vibesys/run-state';
import {SteerMode, SteerTargetKind} from '@vibesys/runs/chat';
import {knownSessions, type ThreadsContext} from './threads.js';

/** Session records project into the transport-neutral chat view consumed by the Run UI. */
export function sessionCore(core: CoreState, context: ThreadsContext | undefined): CoreState {
  const sessions = context ? knownSessions(context) : [];
  const histories = context?.histories ?? {};
  const transcripts = Object.fromEntries(
    sessions.map(session => [
      session.id,
      (histories[session.id]?.turns ?? []).map(
        (turn, index): CoreState['chatTranscript'][number] => ({
          id: String(index * 2 + 1),
          kind: 'assistant',
          content: turn.answer,
          ...(turn.streaming ? {turnId: turn.requestId} : {}),
          ...(turn.failed ? {tone: 'failure'} : {}),
        }),
      ),
    ]),
  );
  return {
    ...core,
    chatThreads: sessions.map(session => ({
      id: session.id,
      title: session.title,
      provider: session.provider,
      model: session.model,
    })),
    lastChatThreadId: sessions.at(-1)?.id ?? null,
    chatTranscripts: transcripts,
    chatTranscript: transcripts['default'] ?? [],
    chatQuestions: Object.fromEntries(
      sessions.map(session => [
        session.id,
        (histories[session.id]?.turns ?? []).map((turn, index) => ({
          id: String(index * 2 + 1),
          text: turn.question,
        })),
      ]),
    ),
    chatProposals: Object.fromEntries(
      sessions.map(session => [
        session.id,
        (histories[session.id]?.proposals ?? []).map(proposal => ({
          id: proposal.id,
          text: proposal.text,
          sequence:
            (histories[session.id]?.turns.findIndex(
              turn => turn.requestId === proposal.requestId,
            ) ?? 0) *
              2 +
            2,
          target: {
            kind: targetKind(proposal.target?.kind ?? SteerTargetKind.RUN),
            id: proposal.target?.id || null,
          },
          mode: mode(proposal.mode),
        })),
      ]),
    ),
  };
}
function targetKind(kind: SteerTargetKind): 'run' | 'role' | 'attempt' | 'item' {
  switch (kind) {
    case SteerTargetKind.RUN:
      return 'run';
    case SteerTargetKind.ROLE:
      return 'role';
    case SteerTargetKind.ATTEMPT:
      return 'attempt';
    case SteerTargetKind.ITEM:
      return 'item';
    default:
      throw new Error('Unknown steer target');
  }
}
function mode(value: SteerMode): 'next_turn' | 'standing' | 'interrupt' {
  switch (value) {
    case SteerMode.NEXT_TURN:
      return 'next_turn';
    case SteerMode.STANDING:
      return 'standing';
    case SteerMode.INTERRUPT:
      return 'interrupt';
    default:
      throw new Error('Unknown steer mode');
  }
}
