import {unprovided} from '@vibesys/machine';
import {assign, type SnapshotFrom, setup} from 'xstate';

/** The legacy bridge acknowledges the request; only a live run confirms it started. */
export const resumeMachine = setup({
  types: {
    context: {} as {error: string | null},
    events: {} as {type: 'resume' | 'started'},
  },
  actors: {resume: unprovided<void, undefined>('resume ended run')},
}).createMachine({
  context: {error: null},
  initial: 'ready',
  on: {started: {target: '.ready'}},
  states: {
    ready: {on: {resume: 'pending'}},
    pending: {
      entry: assign({error: null}),
      initial: 'requesting',
      after: {35000: 'uncertain'},
      states: {
        requesting: {
          invoke: {
            id: 'resume',
            src: 'resume',
            input: undefined,
            onDone: 'waiting',
            onError: {
              target: '#resumeFailed',
              actions: assign({error: ({event}) => String(event.error)}),
            },
          },
        },
        waiting: {},
      },
    },
    failed: {id: 'resumeFailed', on: {resume: 'pending'}},
    uncertain: {on: {resume: 'pending'}},
  },
});

/**
 * What became of Resume: still on its way (with why Resume waits), or a failure to tell the person
 * (a request that did not take is an error, one nobody confirmed a warning).
 */
export type ResumeStatus =
  | {readonly pending: true; readonly reason: string}
  | {
      readonly pending: false;
      readonly tone: 'warn' | 'err';
      readonly title: string;
      readonly detail: string;
    };

export function resumeStatus(snapshot: SnapshotFrom<typeof resumeMachine>): ResumeStatus | null {
  if (snapshot.matches('ready')) return null;
  if (snapshot.matches('failed')) {
    return {
      pending: false,
      tone: 'err',
      title: 'Could not resume the run',
      detail: snapshot.context.error ?? '',
    };
  }
  if (snapshot.matches('uncertain')) {
    return {
      pending: false,
      tone: 'warn',
      title: 'Resume not confirmed',
      detail: 'Check the host before trying again.',
    };
  }
  return {pending: true, reason: 'Waiting for the run to start'};
}
