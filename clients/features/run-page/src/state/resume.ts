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

export interface ResumeStatus {
  readonly pending: boolean;
  readonly message: string;
}

export function resumeStatus(snapshot: SnapshotFrom<typeof resumeMachine>): ResumeStatus | null {
  if (snapshot.matches('ready')) return null;
  if (snapshot.matches('failed'))
    return {pending: false, message: `Could not resume the run: ${snapshot.context.error}`};
  if (snapshot.matches('uncertain'))
    return {pending: false, message: 'Resume not confirmed. Check the host before trying again.'};
  return {pending: true, message: 'Resume requested. Waiting for the run to start…'};
}
