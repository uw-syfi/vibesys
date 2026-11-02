import {describe, expect, test} from 'bun:test';
import {clockFrom} from '@vibesys/machine';
import {FakeDaemon, FixtureSource, generateScenario} from '@vibesys/orchestrator-fixtures';
import {ItemStatus} from '@vibesys/orchestrator-proto';
import {type HostPorts, portsOf} from '@vibesys/orchestrator-source';
import {applyEvent, emptyState, oldestOrdinal} from '@vibesys/orchestrator-state';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {createHostActor, type HostActor} from './host-actor.js';

const scenario = generateScenario({seed: 5, subagents: 9, live: true, depth: 30});
const whole = scenario.events.reduce(applyEvent, emptyState);

/** The agent with the most steps, so it takes several pages to reach its start. */
function longestHistory(): string {
  const counts = new Map<string, number>();
  for (const item of Object.values(whole.items))
    counts.set(item.agentId, (counts.get(item.agentId) ?? 0) + 1);
  return [...counts.entries()].sort((a, b) => b[1] - a[1])[0]?.[0] ?? '';
}

/** Let everything that can run without time run: the streams' microtasks, and nothing else. */
async function flush(): Promise<void> {
  for (let turn = 0; turn < 400; turn++) await Promise.resolve();
}

/** Run to quiet, firing one redial timer at a time like a clock that only moves when asked to. */
async function settle(actor: HostActor, clock: FakeClock, redials = 20): Promise<number> {
  let fired = 0;
  for (;;) {
    await flush();
    const waiting = actor.getSnapshot().value.runs === 'waiting';
    if (!waiting || fired >= redials) return fired;
    clock.runNext();
    fired += 1;
  }
}

function start(
  daemon: FakeDaemon,
  drops: readonly number[],
  ports?: (base: HostPorts) => HostPorts,
) {
  const clock = new FakeClock();
  const base = portsOf(new FixtureSource(daemon, {dropsAfter: drops}));
  const actor = createHostActor(
    {host: 'host-a', ports: ports?.(base) ?? base},
    clockFrom(clock.schedule),
  );
  actor.start();
  return {actor, clock};
}

describe('the host machine', () => {
  test('reads a run from its snapshot and events', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), []);
    await settle(actor, clock);
    const {state} = actor.getSnapshot().context;
    expect(state.sequence).toBe(whole.sequence);
    expect({runs: state.runs, agents: state.agents, turns: state.turns}).toEqual({
      runs: whole.runs,
      agents: whole.agents,
      turns: whole.turns,
    });
    actor.stop();
  });

  test('after any drops it redials from where it was and ends with the whole run', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(fc.integer({min: 1, max: 40}), {maxLength: 4}),
        fc.integer({min: 5, max: 200}),
        async (drops, visible) => {
          const daemon = new FakeDaemon(scenario, visible);
          const {actor, clock} = start(daemon, drops);
          await settle(actor, clock);
          // The run goes on while the client is away.
          for (
            let guard = 0;
            guard < 80 && actor.getSnapshot().context.state.sequence < whole.sequence;
            guard++
          ) {
            daemon.commit(25);
            await settle(actor, clock);
          }
          const {state} = actor.getSnapshot().context;
          expect(state.sequence).toBe(whole.sequence);
          expect(state.agents).toEqual(whole.agents);
          expect(state.turns).toEqual(whole.turns);
          actor.stop();
        },
      ),
      {numRuns: 25},
    );
  });

  test('a frame from a stream it replaced changes nothing', async () => {
    const daemon = new FakeDaemon(scenario, 5);
    const {actor} = start(daemon, [3]);
    await flush();
    daemon.commit(20);
    await flush();
    // The first stream dropped after three frames: the machine is waiting to dial again.
    expect(actor.getSnapshot().value.runs).toBe('waiting');
    const old = actor.getSnapshot().context.runsWatch;
    if (old === null) throw new Error('the machine dialled a stream');
    actor.send({type: 'retry'});
    await flush();
    const mid = actor.getSnapshot().context.state;
    actor.send({
      type: 'runs.frame',
      from: old,
      body: {case: 'event', value: scenario.events[100] as never},
    });
    expect(actor.getSnapshot().context.state).toBe(mid);
    actor.stop();
  });

  test('watching an agent shows its newest items, pages older ones in, and never loads past the first', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), []);
    await settle(actor, clock);
    const agentId = longestHistory();
    actor.send({type: 'agent.open', agentId});
    await flush();
    const firstOldest = oldestOrdinal(actor.getSnapshot().context.state, agentId);
    expect(firstOldest).not.toBeNull();
    for (let page = 0; page < 20; page++) {
      actor.send({type: 'agent.older'});
      await flush();
    }
    expect(oldestOrdinal(actor.getSnapshot().context.state, agentId)).toBe(1);
    expect(actor.getSnapshot().value.older).toBe('idle');
    actor.stop();
  });

  test('older history for an agent that is no longer open is not applied', async () => {
    const gate: {release: () => void} = {release: () => undefined};
    const {actor, clock} = start(new FakeDaemon(scenario), [], base => ({
      ...base,
      listItems: async (...args) => {
        await new Promise<void>(resolve => {
          gate.release = resolve;
        });
        return base.listItems(...args);
      },
    }));
    await settle(actor, clock);
    const [first, second] = Object.values(whole.agents).filter(agent => agent.role === 3);
    actor.send({type: 'agent.open', agentId: first?.id ?? ''});
    await flush();
    const count = (id: string) =>
      Object.values(actor.getSnapshot().context.state.items).filter(i => i.agentId === id).length;
    const loaded = count(first?.id ?? '');
    actor.send({type: 'agent.older'});
    await flush();
    expect(actor.getSnapshot().value.older).toBe('loading');
    actor.send({type: 'agent.open', agentId: second?.id ?? ''});
    gate.release();
    await flush();
    expect(count(first?.id ?? '')).toBe(loaded);
    expect(actor.getSnapshot().value.older).toBe('idle');
    actor.stop();
  });

  test('a message is answered with its receipt, and a refusal is shown by name', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), []);
    await settle(actor, clock);
    const live = Object.values(whole.agents).find(agent => agent.status === 2)?.id ?? '';
    actor.send({type: 'message.send', commandId: 'c1', agentId: live, text: 'hello'});
    await flush();
    const answered = actor.getSnapshot().context.message;
    expect(answered.status).toBe('answered');
    actor.send({type: 'message.send', commandId: 'c2', agentId: 'nobody', text: 'hi'});
    await flush();
    const refused = actor.getSnapshot().context.message;
    expect(refused.status === 'answered' && refused.receipt.refusal).toBeGreaterThan(0);
    actor.stop();
  });

  test('a message that cannot be sent says so and leaves the box usable', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), [], base => ({
      ...base,
      send: () => Promise.reject(new Error('down')),
    }));
    await settle(actor, clock);
    actor.send({type: 'message.send', commandId: 'c1', agentId: 'x', text: 'hello'});
    await flush();
    expect(actor.getSnapshot().context.message.status).toBe('failed');
    actor.send({type: 'message.dismiss'});
    expect(actor.getSnapshot().context.message.status).toBe('idle');
    actor.stop();
  });

  test('progress on a running item shows over it and is gone once the item is final', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), []);
    await settle(actor, clock);
    const running = Object.values(whole.items).find(item => item.status === ItemStatus.RUNNING);
    if (running === undefined) throw new Error('the live fixture has a running item');
    actor.send({type: 'agent.open', agentId: running.agentId});
    await flush();
    expect(actor.getSnapshot().context.state.overlays[running.id]?.text).toBe(
      'Looking at the profile for the next step',
    );
    actor.stop();
  });
});
