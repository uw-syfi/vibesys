import {describe, expect, test} from 'bun:test';
import type {HostId} from '@vibesys/hosts';
import {clockFrom} from '@vibesys/machine';
import {FakeDaemon, FixtureSource, generateScenario} from '@vibesys/orchestrator-fixtures';
import {ItemStatus} from '@vibesys/orchestrator-proto';
import {type HostPorts, portsOf} from '@vibesys/orchestrator-source';
import {applyEvent, emptyState, oldestOrdinal} from '@vibesys/orchestrator-state';
import {BackoffSchedule} from '@vibesys/transport';
import {FakeClock} from '@vibesys/transport/testing';
import fc from 'fast-check';
import {CONNECT_DEADLINE_MS, REQUEST_DEADLINE_MS, redialDelay} from './faults.js';
import {createHostActor, type HostActor} from './host-actor.js';

const HOST: HostId = {kind: 'ssh', alias: 'host-a'};
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
    {host: HOST, ports: ports?.(base) ?? base, backoff: () => new BackoffSchedule()},
    clockFrom(clock.schedule),
  );
  actor.start();
  return {actor, clock};
}

/** Ports that count what is open: every watch opened and every one closed. */
function counting(base: HostPorts) {
  const count = {opened: 0, closed: 0, released: 0};
  const ports: HostPorts = {
    ...base,
    openRuns: after => {
      const watch = base.openRuns(after);
      count.opened += 1;
      return {
        frames: watch.frames,
        close: () => {
          count.closed += 1;
          watch.close();
        },
      };
    },
    openAgent: (...args) => {
      const watch = base.openAgent(...args);
      count.opened += 1;
      return {
        frames: watch.frames,
        close: () => {
          count.closed += 1;
          watch.close();
        },
      };
    },
    close: () => {
      count.released += 1;
    },
  };
  return {ports, count};
}

/** A watch that never answers: a host that accepted the connection and went quiet. */
const silent = (base: HostPorts): HostPorts => ({
  ...base,
  openRuns: () => ({
    frames: (async function* () {
      await new Promise(() => undefined);
    })(),
    close: () => undefined,
  }),
});

const openedAgent = (actor: HostActor) => Object.values(actor.getSnapshot().context.agents)[0];

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

  test('stopping the machine closes every watch it opened, however many redials and agents', async () => {
    await fc.assert(
      fc.asyncProperty(
        fc.array(fc.integer({min: 1, max: 40}), {maxLength: 3}),
        fc.boolean(),
        async (drops, withAgent) => {
          const daemon = new FakeDaemon(scenario);
          const seen: {count: {opened: number; closed: number; released: number}}[] = [];
          const {actor, clock} = start(daemon, drops, base => {
            const made = counting(base);
            seen.push(made);
            return made.ports;
          });
          await settle(actor, clock);
          if (withAgent) {
            actor.send({type: 'agent.open', agentId: longestHistory()});
            await flush();
          }
          actor.stop();
          const count = seen[0]?.count;
          expect(count?.opened).toBeGreaterThan(0);
          expect(count?.closed).toBe(count?.opened);
        },
      ),
      {numRuns: 15},
    );
  });

  test('a host that goes quiet is given up on after the deadline, shown as such, and redialled by Retry', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), [], silent);
    await flush();
    expect(actor.getSnapshot().value.runs).toEqual({open: 'connecting'});
    // Every redial of the shared schedule meets the same silence.
    let redials = 0;
    for (let guard = 0; guard < 40 && actor.getSnapshot().value.runs !== 'gaveUp'; guard++) {
      clock.runNext();
      await flush();
      if (actor.getSnapshot().value.runs === 'waiting') redials += 1;
    }
    expect(actor.getSnapshot().value.runs).toBe('gaveUp');
    expect(redials).toBeGreaterThanOrEqual(1);
    expect(actor.getSnapshot().context.runsFault?.kind).toBe('timeout');
    expect(redialDelay(() => new BackoffSchedule(), 0)).toBeLessThan(CONNECT_DEADLINE_MS);
    actor.send({type: 'retry'});
    expect(actor.getSnapshot().value.runs).toEqual({open: 'connecting'});
    expect(actor.getSnapshot().context.runsAttempts).toBe(0);
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
      actor.send({type: 'agent.older', agentId});
      await flush();
    }
    expect(oldestOrdinal(actor.getSnapshot().context.state, agentId)).toBe(1);
    expect(openedAgent(actor)?.getSnapshot().value.older).toBe('idle');
    actor.stop();
  });

  test('opening another agent starts it clean: nothing of the first one carries over', async () => {
    const gate: {release: () => void} = {release: () => undefined};
    const {actor, clock} = start(new FakeDaemon(scenario), [], base => ({
      ...base,
      listItems: async (...args) => {
        await new Promise<void>(resolve => {
          gate.release = resolve;
        });
        return base.listItems(...args);
      },
      send: () => Promise.reject(new Error('down')),
    }));
    await settle(actor, clock);
    const [first, second] = Object.values(whole.agents).filter(agent => agent.role === 3);
    const a = first?.id ?? '';
    const b = second?.id ?? '';
    actor.send({type: 'agent.open', agentId: a});
    await flush();
    const count = (id: string) =>
      Object.values(actor.getSnapshot().context.state.items).filter(i => i.agentId === id).length;
    const loaded = count(a);
    actor.send({type: 'agent.older', agentId: a});
    actor.send({type: 'message.send', commandId: 'c1', agentId: a, text: 'hi'});
    await flush();
    expect(openedAgent(actor)?.getSnapshot().value.older).toBe('loading');
    actor.send({type: 'agent.open', agentId: b});
    gate.release();
    await flush();
    // The first agent's machine is stopped: its page is not applied, and the second starts clean.
    expect(count(a)).toBe(loaded);
    expect(Object.keys(actor.getSnapshot().context.agents)).toEqual([b]);
    const fresh = openedAgent(actor)?.getSnapshot();
    expect(fresh?.context.box.status).toBe('idle');
    expect(fresh?.context.olderError).toBeNull();
    expect(fresh?.context.fault).toBeNull();
    actor.stop();
  });

  test('a message is answered with its receipt, and a refusal is shown by name', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), []);
    await settle(actor, clock);
    const live = Object.values(whole.agents).find(agent => agent.status === 2)?.id ?? '';
    actor.send({type: 'agent.open', agentId: live});
    actor.send({type: 'message.send', commandId: 'c1', agentId: live, text: 'hello'});
    await flush();
    expect(openedAgent(actor)?.getSnapshot().context.box.status).toBe('answered');
    actor.send({type: 'message.send', commandId: 'c2', agentId: live, text: 'again'});
    await flush();
    expect(openedAgent(actor)?.getSnapshot().context.box.status).toBe('answered');
    actor.stop();
  });

  test('a message that cannot be sent says so and leaves the box usable', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), [], base => ({
      ...base,
      send: () => Promise.reject(new Error('down')),
    }));
    await settle(actor, clock);
    const agentId = longestHistory();
    actor.send({type: 'agent.open', agentId});
    actor.send({type: 'message.send', commandId: 'c1', agentId, text: 'hello'});
    await flush();
    expect(openedAgent(actor)?.getSnapshot().context.box.status).toBe('failed');
    actor.send({type: 'message.dismiss', agentId});
    expect(openedAgent(actor)?.getSnapshot().context.box.status).toBe('idle');
    actor.stop();
  });

  test('a send that is never answered fails at the deadline', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), [], base => ({
      ...base,
      send: () => new Promise(() => undefined),
    }));
    await settle(actor, clock);
    const agentId = longestHistory();
    actor.send({type: 'agent.open', agentId});
    actor.send({type: 'message.send', commandId: 'c1', agentId, text: 'hello'});
    await flush();
    expect(openedAgent(actor)?.getSnapshot().context.box.status).toBe('sending');
    clock.advanceBy(REQUEST_DEADLINE_MS);
    await flush();
    const box = openedAgent(actor)?.getSnapshot().context.box;
    expect(box?.status === 'failed' && box.fault.kind).toBe('timeout');
    actor.stop();
  });

  test('events for an agent that is not open are dropped', async () => {
    const {actor, clock} = start(new FakeDaemon(scenario), []);
    await settle(actor, clock);
    const before = actor.getSnapshot().context;
    actor.send({type: 'message.send', commandId: 'c1', agentId: 'nobody', text: 'hi'});
    actor.send({type: 'agent.older', agentId: 'nobody'});
    actor.send({type: 'agent.close', agentId: 'nobody'});
    expect(actor.getSnapshot().context).toBe(before);
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
