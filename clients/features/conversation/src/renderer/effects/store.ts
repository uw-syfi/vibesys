/** The conversation actor owns requests; stopping it retires every late reply. */
import {actorStore} from '@vibesys/machine';
import type {WebSession} from '@vibesys/run-session';
import type {Store} from '@vibesys/store';
import {createActor, fromPromise} from 'xstate';
import {
  type Availability,
  type ConversationEvent,
  type ConversationState,
  conversationMachine,
  conversationView,
  type Submission,
} from '../../state/index.js';

/** The panel's subscribable state and event input, with explicit request ownership. */
export interface ConversationStore extends Store<ConversationState, ConversationEvent> {
  close(): void;
}

export function createConversationStore(
  session: WebSession | undefined,
  availability: Availability,
): ConversationStore {
  const actor = createActor(
    conversationMachine.provide({
      actors: {
        ask: fromPromise<string | null, Submission>(async ({input, signal}) => {
          if (session === undefined) throw new Error('No live run session');
          const answer = await session.ask(input.text, {signal});
          const folded = session.store.getState().chatTranscript.slice(input.transcriptStart);
          return folded.some(entry => entry.content === answer.answer) ? null : answer.answer;
        }),
        steer: fromPromise<string | null, Submission>(async ({input}) => {
          if (session === undefined) throw new Error('No live run session');
          const ack = await session.command({type: 'command.steer', text: input.text});
          if (ack.kind === 'rejected')
            throw new Error(ack.message ?? 'Server did not acknowledge steering');
          return `Steer ${ack.status}.`;
        }),
      },
    }),
    {input: availability},
  );
  const store = actorStore(actor, conversationView);
  actor.start();
  return {
    ...store,
    close: (): void => {
      actor.stop();
    },
  };
}
