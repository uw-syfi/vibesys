import {create, fromBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  type Agent,
  AgentRole,
  AgentStatus,
  AgentStopStage,
  Compatibility,
  Delivery,
  DispatchRequestSchema,
  Refusal,
  SendMessageSchema,
  StopAgentSchema,
  TurnStatus,
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

const stopOne = (agentId: string): Intent => ({
  case: 'stopAgent',
  value: create(StopAgentSchema, {agentId}),
});

const IN_TRANSIT = new Set([Delivery.QUEUED, Delivery.STEERED, Delivery.NEW_TURN]);

/** What the fake host holds of one agent: the agent, whether it is in a turn, its mail on the way. */
const heldOf = (daemon: FakeDaemon, agentId: string) => {
  const whole = daemon.snapshot();
  return {
    agent: whole.agents.find(agent => agent.id === agentId),
    turning: whole.turns.some(
      turn => turn.agentId === agentId && turn.status === TurnStatus.RUNNING,
    ),
    inTransit: whole.messages.filter(
      message => message.toAgentId === agentId && IN_TRANSIT.has(message.delivery),
    ).length,
  };
};

const beingStopped = (agent: Agent): boolean =>
  agent.stopStage === AgentStopStage.INTERRUPTING || agent.stopStage === AgentStopStage.CLOSING;

/**
 * The host's answer to a stop of `agent` (`stopSubagent` in the orchestrator's core): no such
 * agent, the lead, an agent that is over or already closing, else none.
 */
const hostRefusal = (agent: Agent | undefined): Refusal =>
  agent === undefined
    ? Refusal.AGENT_NOT_FOUND
    : agent.role === AgentRole.LEAD
      ? Refusal.AGENT_IS_LEAD
      : FINAL.has(agent.status) || agent.stopStage === AgentStopStage.CLOSING
        ? Refusal.AGENT_NOT_LIVE
        : Refusal.UNSPECIFIED;

describe('stopping one agent (StopAgent, as the host decides it)', () => {
  // Two subagents still in a turn and two that handed off and wait between turns.
  const run = generateScenario({seed: 11, subagents: 4, live: true});
  const host = () => new FakeDaemon(run);

  const press = (h: HostHarness, agentId: string) =>
    Effect.gen(function* () {
      const id = yield* h.session.commands.submit(stopOne(agentId));
      yield* h.elapse(1);
      return yield* h.read(h.session.commands.progress(id));
    });

  const plans = Schema.Struct({
    seed: between(1, 500),
    subagents: between(1, 6),
    live: Schema.Boolean,
    tree: Schema.Boolean,
    // Each step stops the picked agent, or has the host hear that the picked agent's stop closed
    // its session; the last pick past the agents is an id the host never had.
    steps: Schema.Array(Schema.Struct({finish: Schema.Boolean, pick: between(0, 63)})).check(
      Schema.isMaxLength(10),
    ),
  });

  /** The host hears that an agent's stop closed its session: it is over, with nothing running or owed. */
  const finishChecked = (daemon: FakeDaemon, agentId: string): void => {
    const {agent} = heldOf(daemon, agentId);
    if (agent === undefined || FINAL.has(agent.status) || !beingStopped(agent)) return;
    daemon.finishStop(agentId);
    expect(heldOf(daemon, agentId)).toMatchObject({
      agent: {status: AgentStatus.ENDED, stopStage: AgentStopStage.NONE},
      turning: false,
      inTransit: 0,
    });
  };

  /**
   * A press of Stop is answered as the host answers it. A refused one changes nothing; an accepted
   * one already interrupting a turn changes nothing either, and otherwise interrupts the agent's
   * turn or, between turns, closes its session.
   */
  const pressChecked = (h: HostHarness, daemon: FakeDaemon, agentId: string) =>
    Effect.gen(function* () {
      const before = heldOf(daemon, agentId);
      const progress = yield* press(h, agentId);
      const after = heldOf(daemon, agentId);
      const refusal = hostRefusal(before.agent);
      if (refusal !== Refusal.UNSPECIFIED) {
        expect(progress).toMatchObject({_tag: 'Refused', receipt: {refusal}});
        expect(after.agent).toEqual(before.agent);
        return;
      }
      expect(progress).toMatchObject({_tag: 'Accepted', receipt: {agentId}});
      expect(after.agent?.status).toBe(before.agent?.status);
      expect(after.agent?.stopStage).toBe(
        before.agent?.stopStage === AgentStopStage.INTERRUPTING || before.turning
          ? AgentStopStage.INTERRUPTING
          : AgentStopStage.CLOSING,
      );
    });

  it.effect.prop(
    'every stop is answered as the host answers it, and an accepted one starts stopping the agent',
    {plan: plans},
    ({plan}) =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = new FakeDaemon(
            generateScenario({
              seed: plan.seed,
              subagents: plan.subagents,
              live: plan.live,
              tree: plan.tree,
            }),
          );
          const h = yield* hostHarness(daemon);
          const ids = [...daemon.snapshot().agents.map(agent => agent.id), 'agent-never-held'];
          for (const step of plan.steps) {
            const agentId = ids[step.pick % ids.length] ?? '';
            if (step.finish) finishChecked(daemon, agentId);
            else yield* pressChecked(h, daemon, agentId);
          }
        }).pipe(Random.withSeed(SEED)),
      ),
    {arbitrary: {runs: 25}},
  );

  it.effect(
    'a stop interrupts a working agent, and once its session closes it is over and its mail is rejected',
    () =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = host();
          const h = yield* hostHarness(daemon);
          const working = daemon
            .snapshot()
            .agents.find(
              agent => agent.role !== AgentRole.LEAD && heldOf(daemon, agent.id).turning,
            );
          if (working === undefined) throw new Error('the scenario has no subagent in a turn');
          expect(yield* press(h, working.id)).toMatchObject({_tag: 'Accepted'});
          expect(heldOf(daemon, working.id).agent?.stopStage).toBe(AgentStopStage.INTERRUPTING);
          // A second press while the turn is being interrupted is taken and changes nothing.
          const sequence = daemon.sequence;
          expect(yield* press(h, working.id)).toMatchObject({_tag: 'Accepted'});
          expect(heldOf(daemon, working.id).agent?.stopStage).toBe(AgentStopStage.INTERRUPTING);
          expect(daemon.sequence).toBe(sequence + 1n);
          daemon.finishStop(working.id);
          expect(heldOf(daemon, working.id)).toMatchObject({
            agent: {status: AgentStatus.ENDED, stopStage: AgentStopStage.NONE},
            turning: false,
            inTransit: 0,
          });
          expect(yield* press(h, working.id)).toMatchObject({
            _tag: 'Refused',
            receipt: {refusal: Refusal.AGENT_NOT_LIVE},
          });
        }),
      ),
  );

  it.effect(
    'a stop closes the session of an agent between turns; a stop while it closes is refused',
    () =>
      Effect.scoped(
        Effect.gen(function* () {
          const daemon = host();
          const h = yield* hostHarness(daemon);
          const resting = daemon
            .snapshot()
            .agents.find(
              agent =>
                agent.role !== AgentRole.LEAD &&
                !FINAL.has(agent.status) &&
                !heldOf(daemon, agent.id).turning,
            );
          if (resting === undefined) throw new Error('the scenario has no subagent between turns');
          expect(yield* press(h, resting.id)).toMatchObject({_tag: 'Accepted'});
          expect(heldOf(daemon, resting.id).agent).toMatchObject({
            status: resting.status,
            stopStage: AgentStopStage.CLOSING,
          });
          expect(yield* press(h, resting.id)).toMatchObject({
            _tag: 'Refused',
            receipt: {refusal: Refusal.AGENT_NOT_LIVE},
          });
        }),
      ),
  );

  it.effect('the lead stops only with its run, and an agent the host never had is not found', () =>
    Effect.scoped(
      Effect.gen(function* () {
        const daemon = host();
        const h = yield* hostHarness(daemon);
        const lead = daemon.snapshot().agents.find(agent => agent.role === AgentRole.LEAD);
        if (lead === undefined) throw new Error('the scenario has no lead');
        expect(yield* press(h, lead.id)).toMatchObject({
          _tag: 'Refused',
          receipt: {refusal: Refusal.AGENT_IS_LEAD},
        });
        expect(heldOf(daemon, lead.id).agent).toEqual(lead);
        expect(yield* press(h, 'agent-never-held')).toMatchObject({
          _tag: 'Refused',
          receipt: {refusal: Refusal.AGENT_NOT_FOUND},
        });
      }),
    ),
  );
});
