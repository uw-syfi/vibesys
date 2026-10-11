import {describe, expect, test} from 'bun:test';
import {FakeClock} from '@vibesys/backend-client/testing';
import type {ConnectionStatus} from '../../../common/connection-status.js';
import {exactResumeRunId} from '../../../common/resume-run.js';
import {DEFAULT_CONNECTION_CONFIG, HostConnection} from '../../platform/connection/index.js';
import {FakeLink, record} from '../../platform/connection/testing/fake-helper.js';
import type {RecentRun} from '../../platform/settings/index.js';
import {RunControl} from './run-control.js';

/** A RunControl over one FakeHelper host, with the pages it opened and the runs it remembered. */
function world() {
  const link = new FakeLink();
  link.helper.host.projects.set('/p', ['spsc']);
  const connection = new HostConnection({
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: DEFAULT_CONNECTION_CONFIG,
    scheduleTimeout: new FakeClock().scheduleTimeout,
    log: () => {},
  });
  const remembered: RecentRun[] = [];
  const pages = {opened: 0, closed: 0, woken: 0, confirms: 0};
  const runs = new RunControl({
    connection: async () => connection,
    openPage: () => {
      pages.opened += 1;
      return {wake: () => (pages.woken += 1), close: () => (pages.closed += 1)};
    },
    confirm: async () => {
      pages.confirms += 1;
      return true;
    },
    remember: async run => {
      remembered.push(run);
    },
    changed: () => {},
    log: () => {},
    now: () => 7,
  });
  return {link, connection, runs, remembered, pages};
}

/** Resolve once the attached run's connection reads `kind`. */
async function status(runs: RunControl, kind: ConnectionStatus['kind']): Promise<void> {
  for (let turn = 0; turn < 200 && runs.attached()?.connection.kind !== kind; turn += 1) {
    await new Promise(resolve => setImmediate(resolve));
  }
  expect(runs.attached()?.connection.kind).toBe(kind);
}

describe('RunControl over the FakeHelper', () => {
  test('regression: a start while the host listing is already held binds the run, not an ended one', async () => {
    const w = world();
    // The connection already holds a listing from before the start published its record.
    await new Promise<void>(resolve => w.connection.watchInstances(() => resolve()));
    expect(await w.runs.start({kind: 'local'}, '/p', 'spsc', [])).toBe('started');
    const attached = w.runs.attached();
    expect(attached?.connection.kind).not.toBe('run-ended');
    // The run's identity is its record's, recorded when the start answered.
    const live = w.link.helper.host.instances[0];
    expect(attached?.runId).toBe(live?.run_id);
    expect(w.remembered).toEqual([
      {host: 'local', project: '/p', task: 'spsc', runId: live?.run_id ?? '', attachedAt: 7},
    ]);
    await w.connection[Symbol.asyncDispose]();
  });

  test('a run whose server ends is shown ended, from the pushed registry', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000a', 'run-a', '/p'));
    await w.runs.attach({kind: 'local'}, '00000000000a');
    await status(w.runs, 'connected');
    w.link.helper.end('00000000000a');
    await status(w.runs, 'run-ended');
    await w.connection[Symbol.asyncDispose]();
  });

  test('a resume of a run another server drives attaches to that server, under the same run id', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000b', 'run-b', '/p'));
    await w.runs.resume({kind: 'local'}, '/p', exactResumeRunId('run-b'));
    expect(w.runs.attached()).toMatchObject({instanceId: '00000000000b', runId: 'run-b'});
    expect(w.link.helper.host.instances).toHaveLength(1);
    await w.connection[Symbol.asyncDispose]();
  });

  test('regression: a cancelled start stops its run through the one stop flow, without asking', async () => {
    const w = world();
    const started = w.runs.start({kind: 'local'}, '/p', 'spsc', [], 1);
    w.runs.cancelStart(1);
    expect(await started).toBe('cancelled');
    expect(w.link.helper.host.instances).toHaveLength(0);
    expect(Object.values(w.runs.stops())).toEqual([
      {phase: 'ended', text: 'Run ended', canForce: false},
    ]);
    expect(w.pages.confirms).toBe(0);
    expect(w.runs.attached()).toBeNull();
    await w.connection[Symbol.asyncDispose]();
  });

  test('an attached run holds its connection open until it is detached', async () => {
    const w = world();
    w.link.helper.publish(record('00000000000c', 'run-c', '/p'));
    await w.runs.attach({kind: 'local'}, '00000000000c');
    expect(w.connection.held).toBe(true);
    w.runs.detach();
    expect(w.connection.held).toBe(false);
    await w.connection[Symbol.asyncDispose]();
  });
});
