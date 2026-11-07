/**
 * What a person reads for a host while the connection loop retries. A retry is the loop's own
 * business (the wait, the next dial); it must never change the word a screen shows for the host.
 * Played on the virtual clock against the fake daemon, over generated histories of answers, drops,
 * waits and presses of Retry.
 */
import {describe, expect, it} from '@effect/vitest';
import * as Effect from 'effect/Effect';
import * as Fiber from 'effect/Fiber';
import * as AtomRegistry from 'effect/reactivity/AtomRegistry';
import * as Schema from 'effect/Schema';
import * as TestClock from 'effect/testing/TestClock';
import {type HostConnectionState, hostStatusOf} from '../state/index.js';
import {daemonRecording, FakeDaemon, generateScenario} from '../testing/orchestrator/index.js';
import {quiescent, settle} from '../testing/settle.js';
import {makeFakeTransport} from '../testing/transport/index.js';
import {hostStatusWord} from '../ui/host-status-words.js';
import {makeHostConnection} from './connection.js';
import {RpcStatus, Unavailable} from './errors.js';
import {makeOrchestratorClient} from './orchestrator-client.js';

/** The word of a host with this link and nothing read from it (or something already on screen). */
const word = (connection: HostConnectionState, hasData: boolean): string =>
  hostStatusWord(hostStatusOf({connection, feed: {_tag: 'Opening'}, hasData, staleForMs: null}))
    .label;

/** A link in the loop's own retrying: the wait between attempts, or a dial after a failure. */
const retrying = (state: HostConnectionState): boolean =>
  state._tag === 'Waiting' || (state._tag === 'Connecting' && state.attempt > 0);

type Action =
  | {readonly kind: 'wait'; readonly ms: number}
  | {readonly kind: 'drop'}
  | {readonly kind: 'press'};

/** Play `actions` against a host whose nth Hello is lost when `lost(nth)`; every state the link took. */
const play = (lost: (nth: number) => boolean, actions: readonly Action[]) =>
  Effect.gen(function* () {
    const fake = makeFakeTransport(
      daemonRecording(new FakeDaemon(generateScenario({seed: 1, subagents: 1, live: false})), {
        helloFails: nth => (lost(nth) ? RpcStatus.UNAVAILABLE : undefined),
      }),
    );
    const client = yield* makeOrchestratorClient('local').pipe(Effect.provide(fake.layer));
    const connection = yield* makeHostConnection(client);
    const registry = AtomRegistry.make();
    const states: HostConnectionState[] = [];
    registry.subscribe(connection.state, state => states.push(state), {immediate: true});
    const dial = yield* Effect.forkChild(connection.ready());
    yield* settle;
    for (const action of actions) {
      if (action.kind === 'wait') yield* TestClock.adjust(action.ms);
      if (action.kind === 'press') yield* connection.retry;
      if (action.kind === 'drop') {
        const now = registry.get(connection.state);
        if (now._tag === 'Ready')
          yield* connection.suspect(now.epoch, new Unavailable({detail: 'link lost'}));
      }
      yield* settle;
    }
    yield* Fiber.interrupt(dial);
    return states;
  });

const waits = Schema.Struct({
  kind: Schema.Literal('wait'),
  ms: Schema.Int.check(Schema.isBetween({minimum: 1, maximum: 60_000})),
});
const actions = Schema.Array(
  Schema.Union([
    waits,
    Schema.Struct({kind: Schema.Literal('drop')}),
    Schema.Struct({kind: Schema.Literal('press')}),
  ]),
).check(Schema.isMaxLength(40));
const answers = Schema.Array(Schema.Boolean).check(Schema.isMaxLength(40));

describe('the word of a host while its connection retries', () => {
  it.effect.prop(
    'settles on one word for a host that never answers, and stays on it through every retry',
    {waited: Schema.Array(waits.fields.ms).check(Schema.isMaxLength(40))},
    ({waited}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const states = yield* play(
            () => true,
            waited.map(ms => ({kind: 'wait', ms}) as const),
          );
          // Nothing asked for the host yet reads Waiting; after that, at most a first dial.
          const read = states
            .map(state => word(state, false))
            .filter((each, index, all) => each !== all[index - 1] && each !== 'Waiting');
          expect(['', 'Connecting', 'Unreachable', 'Connecting,Unreachable']).toContain(
            read.join(','),
          );
        }),
      ).pipe(quiescent),
    {arbitrary: {runs: 25}},
  );

  it.effect.prop(
    'never changes between two moments of the loop retrying, whatever was read before',
    {lost: answers, steps: actions},
    ({lost, steps}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const states = yield* play(nth => lost[nth] ?? true, steps);
          states.forEach((state, index) => {
            const next = states[index + 1];
            if (next === undefined || !retrying(state) || !retrying(next)) return;
            expect(word(next, false)).toBe(word(state, false));
            expect(word(next, true)).toBe(word(state, true));
          });
        }),
      ).pipe(quiescent),
    {arbitrary: {runs: 25}},
  );
});
