import {describe, expect, test} from 'bun:test';
import {ConnectionManager} from '@vibesys/connection';
import {FakeLink} from '@vibesys/connection/testing';
import type {RunInputs} from '@vibesys/ipc';
import type {StoredRecentRun} from '@vibesys/settings';
import {FakeClock} from '@vibesys/transport/testing';
import {NewRunMain} from './index.js';

const LOCAL = {kind: 'local'} as const;

function subject() {
  const link = new FakeLink();
  const manager = new ConnectionManager({
    linkFor: () => link,
    appVersion: 'test',
    scheduleTimeout: new FakeClock().scheduleTimeout,
    config: {delaysMs: [1], deadlineMs: 60_000, heartbeatMs: 15_000},
    log: () => {},
  });
  const remembered: StoredRecentRun[] = [];
  const main = new NewRunMain({
    connect: host => manager.acquire(host, '/checkout'),
    remember: async run => {
      remembered.push(run);
    },
    now: () => 42,
  });
  return {link, main, remembered};
}

describe('NewRunMain', () => {
  test('describes the machine and answers the checks for the inputs', async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const machine = await main.answer({kind: 'describe-machine', host: LOCAL});
    expect(machine).toMatchObject({kind: 'offered', value: {hardware: {}}});
    const inputs: RunInputs = {project: '/p', task: 'mpmc', harness: null, options: {}, args: []};
    const answer = (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
      kind: 'offered';
      value: {checks: {id: string; state: string}[]; roles: string[] | null};
    };
    expect(answer.value.checks.map(check => check.state).every(state => state !== 'fail')).toBe(
      true,
    );
    // The helper reports the roles of the run's orchestration, which the fake declares.
    expect(answer.value.roles).toEqual(link.helper.host.roles);
  });

  test('a helper without run_roles is not asked for roles, and none are reported', async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    link.helper.features.delete('run_roles');
    const inputs: RunInputs = {project: '/p', task: 'mpmc', harness: null, options: {}, args: []};
    const answer = (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
      kind: 'offered';
      value: {roles: string[] | null};
    };
    expect(answer.value.roles).toBeNull();
    const asked = link.helper.requests.filter(request => request.type === 'run.preflight');
    expect(asked.map(request => 'with_roles' in request)).toEqual([false]);
  });

  test('starts a run with typed options and records it in Recent', async () => {
    const {link, main, remembered} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const started = (await main.answer({
      kind: 'start-run',
      host: LOCAL,
      inputs: {project: '/p', task: 'mpmc', harness: null, options: {rounds: 2}, args: []},
    })) as {runId: string; instanceId: string};
    expect(started.runId).not.toBe('');
    expect(remembered).toEqual([
      {
        host: 'local',
        project: '/p',
        task: 'mpmc',
        runId: started.runId,
        instanceId: started.instanceId,
        attachedAt: 42,
        start: {harness: null, options: {rounds: 2}, args: []},
      },
    ]);
  });

  test("answers the features the machine's helper agreed to in hello", async () => {
    const {link, main} = subject();
    link.helper.features.delete('run_options');
    link.helper.features.delete('examples');
    const features = (await main.answer({kind: 'helper-features', host: LOCAL})) as string[];
    expect(features).toContain('preflight');
    expect(features).not.toContain('run_options');
    expect(features).not.toContain('examples');
  });

  test('a failed request says what it was doing', async () => {
    const {main} = subject();
    await expect(
      main.answer({kind: 'copy-out', host: LOCAL, source: '/missing', destination: '/own'}),
    ).rejects.toThrow('Could not copy /missing out');
  });
});
