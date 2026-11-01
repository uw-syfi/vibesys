/**
 * The Limits tab's control state, as a machine (docs/contributing/clients-architecture.md,
 * "State"). It asks every connected host for each provider's plan limits and keeps what came back:
 *
 * - Opening the tab asks, unless a question was asked in the last five minutes (the floor: a
 *   provider's CLI is started on the host for each read, so a person flicking between tabs must not
 *   start them again). The floor holds after a failed question too, so a host that is down is not
 *   asked in a loop. The host answers a reading under five minutes old as it is.
 * - Refresh asks now and asks the CLIs, whatever the floor: the person pressed it.
 * - A question that is still running when another is asked is stopped; a deadline ends one a host
 *   never answers. A failed question keeps the figures already shown and says so.
 *
 * Time arrives on the events: the machine reads no clock.
 */
import type {HostKey} from '@vibesys/hosts';
import {unprovided} from '@vibesys/machine';
import {assign, setup} from 'xstate';
import type {HostLimits} from './limits-view.js';

/** What a question came back with. */
export interface LimitsAnswer {
  readonly replies: readonly HostLimits[];
  /** Hosts that could not be read. */
  readonly unreachable: readonly HostKey[];
  /** Hosts whose VibeSys does not offer the question. */
  readonly unsupported: readonly HostKey[];
}

export interface LimitsContext {
  /** The figures last read; null before any. */
  readonly answer: LimitsAnswer | null;
  /** When the last question was asked (milliseconds); null before any. */
  readonly askedAt: number | null;
  /** How old a stored reading the question lets a host answer as it is, in seconds. */
  readonly maxAgeS: number;
  /** Whether the last question failed. */
  readonly failed: boolean;
}

export type LimitsEvent =
  | {readonly type: 'open'; readonly now: number}
  | {readonly type: 'refresh'; readonly now: number};

/** What the question is told: how old a stored reading a host may answer as it is. */
export interface LimitsQuestion {
  readonly maxAgeS: number;
}

/** How long after a question the tab opening asks again, in milliseconds. */
export const LIMITS_FLOOR_MS = 5 * 60_000;
/** How long a stored reading may be answered as it is when the tab opens, in seconds. */
export const LIMITS_STORED_AGE_S = 300;
/** How long a question may take, in milliseconds: a little over the host's own bound. */
export const LIMITS_DEADLINE_MS = 45_000;

export const limitsMachine = setup({
  types: {context: {} as LimitsContext, events: {} as LimitsEvent},
  actors: {check: unprovided<LimitsAnswer, LimitsQuestion>('check')},
  actions: {
    ask: assign((_, params: {readonly now: number; readonly maxAgeS: number}) => ({
      askedAt: params.now,
      maxAgeS: params.maxAgeS,
    })),
    show: assign((_, params: {readonly answer: LimitsAnswer}) => ({
      answer: params.answer,
      failed: false,
    })),
    fail: assign({failed: true}),
  },
  guards: {
    pastFloor: ({context}, params: {readonly now: number}) =>
      context.askedAt === null || params.now - context.askedAt >= LIMITS_FLOOR_MS,
  },
  delays: {deadline: LIMITS_DEADLINE_MS},
}).createMachine({
  id: 'usage-limits',
  context: {answer: null, askedAt: null, maxAgeS: LIMITS_STORED_AGE_S, failed: false},
  initial: 'idle',
  states: {
    idle: {
      on: {
        open: {
          guard: {type: 'pastFloor', params: ({event}) => ({now: event.now})},
          target: 'checking',
          actions: {
            type: 'ask',
            params: ({event}) => ({now: event.now, maxAgeS: LIMITS_STORED_AGE_S}),
          },
        },
        refresh: {
          target: 'checking',
          actions: {type: 'ask', params: ({event}) => ({now: event.now, maxAgeS: 0})},
        },
      },
    },
    checking: {
      invoke: {
        id: 'check',
        src: 'check',
        input: ({context}) => ({maxAgeS: context.maxAgeS}),
        onDone: {
          target: 'idle',
          actions: {type: 'show', params: ({event}) => ({answer: event.output})},
        },
        onError: {target: 'idle', actions: 'fail'},
      },
      after: {deadline: {target: 'idle', actions: 'fail'}},
      on: {
        refresh: {
          target: 'checking',
          reenter: true,
          actions: {type: 'ask', params: ({event}) => ({now: event.now, maxAgeS: 0})},
        },
      },
    },
  },
});
