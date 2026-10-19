import {describe, expect, test} from 'bun:test';
import {ConnectionManager} from '@vibesys/connection';
import {FakeLink} from '@vibesys/connection/testing';
import type {RunInputs} from '@vibesys/ipc';
import {exampleCatalogue} from '@vibesys/run-fixtures';
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
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      args: [],
    };
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
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      args: [],
    };
    const answer = (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
      kind: 'offered';
      value: {roles: string[] | null};
    };
    expect(answer.value.roles).toBeNull();
    const asked = link.helper.requests.filter(request => request.type === 'run.preflight');
    expect(asked.map(request => 'with_roles' in request)).toEqual([false]);
  });

  test("reports the run's backend, and the checks' typed causes, from a helper that has run_backend", async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    link.helper.host.backend = 'cpu';
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      args: [],
    };
    const answer = (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
      value: {backend: string | null};
    };
    expect(answer.value.backend).toBe('cpu');
  });

  test('a helper without run_backend is not asked for the backend, and none is reported', async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    link.helper.features.delete('run_backend');
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      args: [],
    };
    const answer = (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
      value: {backend: string | null};
    };
    expect(answer.value.backend).toBeNull();
    const asked = link.helper.requests.filter(request => request.type === 'run.preflight');
    expect(asked.map(request => 'with_backend' in request)).toEqual([false]);
  });

  test('asks for the project_clean check only from a helper that has it', async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      args: [],
    };
    const ids = async () =>
      (
        (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
          value: {checks: {id: string}[]};
        }
      ).value.checks.map(check => check.id);
    expect(await ids()).toContain('project_clean');
    link.helper.features.delete('project_clean');
    expect(await ids()).not.toContain('project_clean');
  });

  test('asks for the typed causes of failed settings only from a helper that has run_causes', async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    link.helper.host.preflight = link.helper.host.preflight.map(check =>
      check.id === 'settings_valid'
        ? {
            ...check,
            state: 'fail' as const,
            detail: 'the extra arguments include some this search does not take',
            cause: {kind: 'option_not_accepted' as const, option: 'rounds' as const},
          }
        : check,
    );
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      args: [],
    };
    const settings = async () =>
      (
        (await main.answer({kind: 'preflight', host: LOCAL, inputs})) as {
          value: {checks: {id: string; cause?: {kind: string}}[]};
        }
      ).value.checks.find(check => check.id === 'settings_valid');
    expect((await settings())?.cause?.kind).toBe('option_not_accepted');
    link.helper.features.delete('run_causes');
    expect((await settings())?.cause).toBeUndefined();
    const asked = link.helper.requests.filter(request => request.type === 'run.preflight');
    expect(asked.map(request => 'with_causes' in request)).toEqual([true, false]);
  });

  test('starts a run with typed options and records it in Recent', async () => {
    const {link, main, remembered} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const started = (await main.answer({
      kind: 'start-run',
      host: LOCAL,
      inputs: {
        project: '/p',
        task: 'mpmc',
        harness: null,
        options: {rounds: 2},
        agents: {},
        args: [],
      },
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
        start: {harness: null, options: {rounds: 2}, agents: {}, args: []},
      },
    ]);
  });

  test('sends an agent per role to a helper that has run_role_agents, on the check and on the start', async () => {
    const {link, main, remembered} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const role = link.helper.host.roles[0] ?? '';
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: {provider: 'codex', model: 'gpt-a'},
      options: {},
      agents: {[role]: {provider: 'codex', model: 'gpt-b'}},
      args: [],
    };
    await main.answer({kind: 'preflight', host: LOCAL, inputs});
    await main.answer({kind: 'start-run', host: LOCAL, inputs});
    const asked = link.helper.requests.filter(
      request => request.type === 'run.preflight' || request.type === 'run.start',
    );
    expect(asked.map(request => 'agents' in request && request.agents)).toEqual([
      inputs.agents,
      inputs.agents,
    ]);
    expect(remembered[0]?.start?.agents).toEqual(inputs.agents);
  });

  test('a helper without run_role_agents is never sent agents: the request is refused, nothing starts', async () => {
    const {link, main, remembered} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    link.helper.features.delete('run_role_agents');
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {judge: {provider: 'claude', model: 'claude-sonnet-4-5'}},
      args: [],
    };
    await expect(main.answer({kind: 'start-run', host: LOCAL, inputs})).rejects.toThrow(
      'different agent for a role',
    );
    await expect(main.answer({kind: 'preflight', host: LOCAL, inputs})).rejects.toThrow(
      'different agent for a role',
    );
    expect(link.helper.requests.some(request => request.type === 'run.start')).toBe(false);
    expect(remembered).toEqual([]);
  });

  test("reads a project task's goal from a helper that has project_goal, and says so when it cannot", async () => {
    const {link, main} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const goal = {
      task: 'mpmc',
      objectives: [{metric: 'ops', direction: 'max' as const}],
      goal: 'Go fast.',
      goal_truncated: false,
    };
    link.helper.host.goals.set('/p mpmc', goal);
    const read = () => main.answer({kind: 'task-goal', host: LOCAL, project: '/p', task: 'mpmc'});
    expect(await read()).toEqual({kind: 'offered', value: goal});
    link.helper.features.delete('project_goal');
    expect(await read()).toEqual({kind: 'unsupported'});
    link.helper.features.add('project_goal');
    await expect(
      main.answer({kind: 'task-goal', host: LOCAL, project: '/p', task: 'nope'}),
    ).rejects.toThrow('Could not read the goal of nope');
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

  test('asks what each example is about only from a helper that has example_catalogue', async () => {
    const {link, main} = subject();
    link.helper.host.examples = exampleCatalogue;
    const described = async () =>
      (
        (await main.answer({kind: 'examples', host: LOCAL})) as {
          value: {examples: {task_details?: unknown}[]};
        }
      ).value.examples.some(example => example.task_details !== undefined);
    expect(await described()).toBe(true);
    link.helper.features.delete('example_catalogue');
    expect(await described()).toBe(false);
    const asked = link.helper.requests.filter(request => request.type === 'examples.list');
    expect(asked.map(request => 'with_catalogue' in request)).toEqual([true, false]);
  });

  test('a failed request says what it was doing', async () => {
    const {main} = subject();
    await expect(
      main.answer({kind: 'copy-out', host: LOCAL, source: '/missing', destination: '/own'}),
    ).rejects.toThrow('Could not copy /missing out');
  });
});
