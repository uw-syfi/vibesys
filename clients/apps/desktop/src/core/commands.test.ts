import {create, fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  AgentStatus,
  Compatibility,
  DispatchRequestSchema,
  SendMessageSchema,
} from '@vibesys/orchestrator-proto';
import * as Effect from 'effect/Effect';
import * as Random from 'effect/Random';
import * as Schema from 'effect/Schema';
import type {CommandProgress} from '../state/index.js';
import {
  type DispatchFate,
  FakeDaemon,
  faultsOn,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {Recording} from '../testing/transport/index.js';
import type {Intent} from './commands.js';
import {RpcStatus} from './errors.js';

const SEED = 20261103;
const FINAL = new Set([AgentStatus.ENDED, AgentStatus.FAILED, AgentStatus.LOST]);
const scenario = generateScenario({seed: 11, subagents: 2, live: true});
const freshDaemon = () => new FakeDaemon(scenario);

const liveAgentId = (daemon: FakeDaemon): string => {
  const agent = daemon.snapshot().agents.find(a => !FINAL.has(a.status));
  if (agent === undefined) throw new Error('the scenario has no live agent');
  return agent.id;
};

const say = (agentId: string, text: string): Intent => ({
  case: 'sendMessage',
  value: create(SendMessageSchema, {agentId, text}),
});

/** The command id of every Dispatch the daemon was asked for, in order, with its call number. */
const dispatched = (h: HostHarness) =>
  h.fake
    .calls()
    .filter(call => call.target.method === methodPathOf('Dispatch'))
    .map(call => ({
      nth: call.nth,
      id: fromBinary(DispatchRequestSchema, call.target.request).command?.id ?? '',
    }));

const answered = (progress: CommandProgress | undefined) =>
  progress?._tag === 'Accepted' || progress?._tag === 'Refused';

const between = (minimum: number, maximum: number) =>
  Schema.Int.check(Schema.isBetween({minimum, maximum}));

/** `base` with every Dispatch taking `millis` of virtual time before it answers. */
const slowDispatch =
  (millis: number) =>
  (base: Recording): Recording => ({
    respond: (target, nth) => {
      const steps = base.respond(target, nth);
      return target.method === methodPathOf('Dispatch') && millis > 0
        ? [{sleep: `${millis} millis`}, ...steps]
        : steps;
    },
  });

describe('commands (P6: no mutation is sent twice by the app)', () => {
  // The link faults of a plan: Hellos that fail before the first Ready, a run stream that is
  // mounted and drops after some frames (an epoch change while a command may be in flight), and
  // a Dispatch that takes a while to answer.
  const plans = Schema.Struct({
    fates: Schema.Array(Schema.Literals(['ok', 'lose-request', 'lose-reply'])).check(
      Schema.isMaxLength(8),
    ),
    actions: between(1, 4),
    retries: between(0, 2),
    helloFails: Schema.Array(between(0, 3)).check(Schema.isMaxLength(2)),
    mounted: Schema.Boolean,
    drops: Schema.Array(between(0, 4)).check(Schema.isMaxLength(3)),
    dispatchMillis: Schema.Literals([0, 400, 3000]),
  });
  type Plan = typeof plans.Type;
  const fateOf = (plan: Plan, nth: number): DispatchFate => plan.fates[nth] ?? 'ok';

  const play = (plan: Plan) =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = freshDaemon();
        const agent = liveAgentId(daemon);
        const failing = new Set(plan.helloFails);
        const h = yield* hostHarness(daemon, {
          dispatch: nth => fateOf(plan, nth),
          closeStreams: true,
          helloFails: nth => (failing.has(nth) ? RpcStatus.UNAVAILABLE : undefined),
          wrap: base =>
            faultsOn('WatchRuns', {dropAfter: plan.drops, duplicate: []})(
              slowDispatch(plan.dispatchMillis)(base),
            ),
        });
        if (plan.mounted) h.mount(h.session.runs.watch);
        const ids: string[] = [];
        for (let action = 0; action < plan.actions; action += 1) {
          ids.push(yield* h.session.commands.submit(say(agent, `message ${action}`)));
        }
        // Long enough for any timer the app might (wrongly) use to re-send.
        yield* h.elapse(300, 10);
        for (let round = 0; round < plan.retries; round += 1) {
          for (const id of ids) yield* h.session.commands.retry(id);
          yield* h.elapse(300, 10);
        }
        const progress = yield* Effect.forEach(ids, id => h.read(h.session.commands.progress(id)));
        return {h, ids, progress};
      }).pipe(Random.withSeed(SEED)),
    );

  it.effect.prop(
    'sends once per action plus once per explicit retry, always under the same id',
    {plan: plans},
    ({plan}) =>
      Effect.gen(function* () {
        const {h, ids, progress} = yield* play(plan);
        const calls = dispatched(h);
        expect(new Set(ids).size).toBe(ids.length);
        for (const call of calls) expect(ids).toContain(call.id);
        for (const id of ids) {
          const sent = calls.filter(call => call.id === id);
          expect(sent.length).toBeGreaterThanOrEqual(1);
          expect(sent.length).toBeLessThanOrEqual(1 + plan.retries);
        }
        if (plan.retries === 0) expect(calls).toHaveLength(plan.actions);
        ids.forEach((id, index) => {
          const fates = calls.filter(call => call.id === id).map(call => fateOf(plan, call.nth));
          const replied = fates.includes('ok');
          // The daemon holds a receipt once it carried a command out, and a mounted run stream
          // may carry that receipt to a command whose reply was lost.
          const receipted = replied || fates.includes('lose-reply');
          if (replied) expect(answered(progress[index])).toBe(true);
          else if (plan.mounted && receipted)
            expect(['Unconfirmed', 'Accepted', 'Refused']).toContain(progress[index]?._tag);
          else expect(progress[index]?._tag).toBe('Unconfirmed');
          if (answered(progress[index])) expect(receipted).toBe(true);
        });
      }),
    {arbitrary: {runs: 30}},
  );

  it.effect('settles a command whose reply was lost from the receipt on the run stream', () =>
    Effect.gen(function* () {
      const daemon = freshDaemon();
      const h = yield* hostHarness(daemon, {
        dispatch: () => 'lose-reply',
        closeStreams: true,
      });
      h.mount(h.session.runs.watch);
      const id = yield* h.session.commands.submit(say(liveAgentId(daemon), 'hello'));
      yield* h.elapse(120, 5);
      const settled = yield* h.read(h.session.commands.progress(id));
      expect(answered(settled)).toBe(true);
      expect(dispatched(h)).toHaveLength(1);
    }),
  );

  it.effect('retries an unconfirmed command under its own id, and only that', () =>
    Effect.gen(function* () {
      const daemon = freshDaemon();
      const h = yield* hostHarness(daemon, {
        dispatch: nth => (nth === 0 ? 'lose-request' : 'ok'),
      });
      const id = yield* h.session.commands.submit(say(liveAgentId(daemon), 'hello'));
      yield* h.elapse(1);
      expect(yield* h.read(h.session.commands.progress(id))).toMatchObject({_tag: 'Unconfirmed'});
      yield* h.session.commands.retry(id);
      yield* h.elapse(1);
      expect(answered(yield* h.read(h.session.commands.progress(id)))).toBe(true);
      expect(dispatched(h).map(call => call.id)).toEqual([id, id]);
      // Answered commands, and ids never issued, are left alone.
      yield* h.session.commands.retry(id);
      yield* h.session.commands.retry('never-issued');
      yield* h.elapse(1);
      expect(dispatched(h)).toHaveLength(2);
      expect(yield* h.read(h.session.commands.progress('never-issued'))).toBeUndefined();
    }),
  );

  it.effect(
    'a command that finds the link dropped makes the connection redial, with no feed mounted',
    () =>
      Effect.gen(function* () {
        const daemon = freshDaemon();
        const h = yield* hostHarness(daemon, {
          dispatch: nth => (nth === 0 ? 'lose-request' : 'ok'),
        });
        const id = yield* h.session.commands.submit(say(liveAgentId(daemon), 'hello'));
        yield* h.elapse(0);
        expect(yield* h.read(h.session.commands.progress(id))).toMatchObject({_tag: 'Unconfirmed'});
        expect(yield* h.read(h.session.connection.state)).toMatchObject({_tag: 'Waiting'});
        yield* h.elapse(60);
        expect(yield* h.read(h.session.connection.state)).toMatchObject({_tag: 'Ready', epoch: 2});
        expect(dispatched(h)).toHaveLength(1);
      }),
  );

  it.effect('retries made at the same moment send the command once', () =>
    Effect.gen(function* () {
      const daemon = freshDaemon();
      const h = yield* hostHarness(daemon, {
        dispatch: nth => (nth === 0 ? 'lose-request' : 'ok'),
      });
      const id = yield* h.session.commands.submit(say(liveAgentId(daemon), 'hello'));
      yield* h.elapse(1);
      const retries = Array.from({length: 4}, () => h.session.commands.retry(id));
      yield* Effect.all(retries, {concurrency: 'unbounded'});
      yield* h.elapse(60);
      expect(dispatched(h).map(call => call.id)).toEqual([id, id]);
    }),
  );

  it.effect('does not send into a host that is incompatible', () =>
    Effect.gen(function* () {
      const daemon = freshDaemon();
      const h = yield* hostHarness(daemon, {compatibility: Compatibility.DAEMON_TOO_OLD});
      const id = yield* h.session.commands.submit(say(liveAgentId(daemon), 'hello'));
      yield* h.elapse(60, 5);
      expect(yield* h.read(h.session.commands.progress(id))).toMatchObject({
        _tag: 'Failed',
        fault: {kind: 'Unavailable', retryable: false},
      });
      expect(dispatched(h)).toHaveLength(0);
    }),
  );
});
