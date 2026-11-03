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
import fc from 'fast-check';
import type {CommandProgress} from '../state/index.js';
import {
  type DispatchFate,
  FakeDaemon,
  generateScenario,
  type HostHarness,
  hostHarness,
  methodPathOf,
} from '../testing/orchestrator/index.js';
import type {Intent} from './commands.js';

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

describe('commands (P6: no mutation is sent twice by the app)', () => {
  interface Plan {
    readonly fates: ReadonlyArray<DispatchFate>;
    readonly actions: number;
    readonly retries: number;
  }
  const plans = fc.record({
    fates: fc.array(fc.constantFrom<DispatchFate>('ok', 'lose-request', 'lose-reply'), {
      maxLength: 8,
    }),
    actions: fc.integer({min: 1, max: 4}),
    retries: fc.integer({min: 0, max: 2}),
  });

  const play = (plan: Plan) =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = freshDaemon();
        const agent = liveAgentId(daemon);
        const h = yield* hostHarness(daemon, {dispatch: nth => plan.fates[nth] ?? 'ok'});
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

  it.effect('sends once per action plus once per explicit retry, always under the same id', () =>
    Effect.gen(function* () {
      for (const plan of fc.sample(plans, {seed: SEED, numRuns: 30})) {
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
        // An outcome is known exactly when a reply arrived for one of the action's sends.
        ids.forEach((id, index) => {
          const fates = calls
            .filter(call => call.id === id)
            .map(call => plan.fates[call.nth] ?? 'ok');
          const replied = fates.includes('ok');
          expect(answered(progress[index])).toBe(replied);
          if (!replied) expect(progress[index]?._tag).toBe('Unconfirmed');
        });
      }
    }),
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
