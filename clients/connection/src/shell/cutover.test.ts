/**
 * Cutover check, deleted with the hand-written shell: both `HostConnection` shells pass the contract
 * suite over a `FakeLink` and `FakeClock`, and walk the same phases through it and through the
 * recovery scenarios.
 */
import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/transport/testing';
import type {ConnectionConfig, Phase} from '../state/host-connection.js';
import {FakeLink, record} from '../testing/fake-helper.js';
import {hostConnectionContract, phaseReached} from '../testing/host-connection-contract.js';
import {HostConnection} from './host-connection.js';
import {HostConnection as LegacyHostConnection} from './legacy-host-connection.js';

const CONFIG: ConnectionConfig = {delaysMs: [500, 1_000], deadlineMs: 60_000, heartbeatMs: 15_000};

type Shell = 'legacy' | 'machine';
const SHELLS: readonly Shell[] = ['legacy', 'machine'];

/** The phases each connection walked, per shell, in the order the connections were made. */
const traces: Record<Shell, Phase[][]> = {legacy: [], machine: []};

function connection(shell: Shell, link: FakeLink, clock = new FakeClock()): HostConnection {
  const options = {
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
  };
  // test-isolation: the hand-written shell is the cutover's reference, typed as its successor.
  const made =
    shell === 'machine'
      ? new HostConnection(options)
      : (new LegacyHostConnection(options) as unknown as HostConnection);
  const trace: Phase[] = [];
  traces[shell].push(trace);
  made.onPhase(phase => trace.push(phase));
  return made;
}

for (const shell of SHELLS) {
  hostConnectionContract(`fake, ${shell} shell`, {
    working: () => connection(shell, new FakeLink()),
    broken: () => {
      const link = new FakeLink();
      link.helper.bootstrapExit = {code: 65, stdout: 'nodir'};
      return connection(shell, link);
    },
  });

  describe(`recovery scenarios (${shell} shell)`, () => {
    test('a dropped link is checked and restored', async () => {
      const link = new FakeLink();
      await using made = connection(shell, link);
      await made.ready();
      link.helper.drop();
      await phaseReached(made, 'reconnecting');
      await phaseReached(made, 'ready');
    });

    test('a link that stays down walks the schedule to offline, and a wake restores it', async () => {
      const clock = new FakeClock();
      const link = new FakeLink();
      await using made = connection(shell, link, clock);
      await made.ready();
      link.linkFault = {cause: 'link', detail: 'down'};
      link.helper.drop();
      for (const delay of CONFIG.delaysMs) {
        await clock.waitUntilScheduled(delay);
        clock.advanceBy(delay);
      }
      await phaseReached(made, 'offline');
      link.linkFault = null;
      made.dispatch({type: 'wake'});
      await phaseReached(made, 'ready');
    });

    test('a refused sign-in waits for the user', async () => {
      const link = new FakeLink();
      link.linkFault = {cause: 'auth', detail: 'Permission denied'};
      await using made = connection(shell, link);
      await made.ready().catch(() => {});
      link.linkFault = null;
      made.dispatch({type: 'user-retry'});
      await phaseReached(made, 'ready');
    });

    test('a run channel the link broke is checked with a fresh dial', async () => {
      const link = new FakeLink();
      link.helper.publish(record('00000000000e', 'run-e'));
      link.helper.host.runs.set('00000000000e', stream =>
        stream.onLine(() => stream.end('server_stalled')),
      );
      await using made = connection(shell, link);
      const {stream} = await made.openRun('00000000000e');
      stream.on('error', () => {});
      stream.resume();
      stream.write('go\n');
      await phaseReached(made, 'reconnecting');
      await phaseReached(made, 'ready');
    });
  });
}

test('both shells walk the same phases', () => {
  expect(traces.machine.length).toBe(traces.legacy.length);
  expect(traces.machine.length).toBeGreaterThan(0);
  expect(traces.machine).toEqual(traces.legacy);
});
