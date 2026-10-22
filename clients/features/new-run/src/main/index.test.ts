import {describe, expect, test} from 'bun:test';
import {ConnectionError, ConnectionManager} from '@vibesys/connection';
import {FakeLink} from '@vibesys/connection/testing';
import type {ExampleCreation, RunInputs} from '@vibesys/ipc';
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
  test('a connection failure keeps its typed cause when it is worded for the screen', async () => {
    const main = new NewRunMain({
      connect: async () => {
        throw new ConnectionError('incompatible', 'the host helper for /srv/x exited with 1');
      },
      remember: async () => undefined,
      now: () => 42,
    });
    const failure = await main
      .answer({kind: 'examples', host: LOCAL})
      .catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(ConnectionError);
    expect((failure as ConnectionError).reason).toBe('incompatible');
    expect((failure as ConnectionError).message).toContain('exited with 1');
  });

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
      measure_on: {kind: 'same_machine'},
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
      measure_on: {kind: 'same_machine'},
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
      measure_on: {kind: 'same_machine'},
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
      measure_on: {kind: 'same_machine'},
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
      measure_on: {kind: 'same_machine'},
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
      measure_on: {kind: 'same_machine'},
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

  test('a v2 start uses the registry and does not ask the helper', async () => {
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
      transportV2: async () => true,
      registryStart: async () => ({runId: 'run-9', instanceId: 'inst-9'}),
    });
    const started = await main.answer({
      kind: 'start-run',
      host: LOCAL,
      inputs: {
        project: '/p',
        task: 'mpmc',
        harness: null,
        options: {},
        agents: {},
        measure_on: {kind: 'same_machine'},
        args: [],
      },
    });
    expect(started).toEqual({runId: 'run-9', instanceId: 'inst-9'});
    expect(link.helper.requests.some(request => request.type === 'run.start')).toBe(false);
    expect(remembered[0]?.runId).toBe('run-9');
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
        measure_on: {kind: 'same_machine'},
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
        start: {
          harness: null,
          options: {rounds: 2},
          agents: {},
          measure_on: {kind: 'same_machine'},
          args: [],
        },
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
      measure_on: {kind: 'same_machine'},
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
      measure_on: {kind: 'same_machine'},
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

  test('sends where to measure only to a helper that has measurement_target, on the check and the start', async () => {
    const {link, main, remembered} = subject();
    link.helper.host.projects.set('/p', ['mpmc']);
    const inputs: RunInputs = {
      project: '/p',
      task: 'mpmc',
      harness: null,
      options: {},
      agents: {},
      measure_on: {kind: 'slurm', cluster: 'lab-h100'},
      args: [],
    };
    const sent = () =>
      link.helper.requests
        .filter(request => request.type === 'run.preflight' || request.type === 'run.start')
        .map(request => ('measure_on' in request ? request.measure_on : 'absent'));
    await main.answer({kind: 'preflight', host: LOCAL, inputs});
    await main.answer({kind: 'start-run', host: LOCAL, inputs});
    expect(sent()).toEqual([inputs.measure_on, inputs.measure_on]);
    expect(remembered[0]?.start?.measure_on).toEqual(inputs.measure_on);
    // Measuring on this machine is the default and sends nothing.
    const here = {...inputs, measure_on: {kind: 'same_machine' as const}};
    await main.answer({kind: 'preflight', host: LOCAL, inputs: here});
    expect(sent().at(-1)).toBe('absent');
    // A helper without the feature is refused, loudly, before anything starts.
    link.helper.features.delete('measurement_target');
    await expect(main.answer({kind: 'start-run', host: LOCAL, inputs})).rejects.toThrow(
      'cannot measure on a cluster or cloud',
    );
    await expect(main.answer({kind: 'preflight', host: LOCAL, inputs})).rejects.toThrow(
      'cannot measure on a cluster or cloud',
    );
    expect(remembered).toHaveLength(1);
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

  test('a failed request says what it was doing, without naming a path', async () => {
    const {main} = subject();
    const failure = await main
      .answer({kind: 'copy-out', host: LOCAL, source: '/missing', destination: '/own'})
      .then(
        () => null,
        (error: Error) => error,
      );
    expect(failure?.message).toStartWith('Could not copy the project out');
    expect(failure?.message).not.toContain('/missing');
    expect(failure?.message).not.toContain('/own');
  });

  describe('what an example would find at its destination', () => {
    const STACK = 'data-structures/stack';
    const TASKS = ['mpmc', 'spsc'];
    const ask = (main: ReturnType<typeof subject>['main'], destination: string) =>
      main.answer({kind: 'example-destination', host: LOCAL, example: STACK, destination});
    const create = (main: ReturnType<typeof subject>['main'], destination: string) =>
      main.answer({kind: 'create-from-example', host: LOCAL, example: STACK, destination});

    /** One destination of each kind on a helper whose catalogue has the stack example. */
    function world() {
      const made = subject();
      const {host} = made.link.helper;
      host.examples = exampleCatalogue;
      host.directories.set('~/empty', []);
      host.directories.set('~/junk', ['notes']);
      host.projects.set('~/own', TASKS);
      host.directories.set('~/own', []);
      host.projects.set('~/other', ['default']);
      host.directories.set('~/other', []);
      return made;
    }

    const STATES = [
      ['~/absent', {state: 'absent', project: null}],
      ['~/empty', {state: 'empty', project: null}],
      ['~/own', {state: 'example_project', project: '~/own'}],
      ['~/other', {state: 'occupied', project: null}],
      ['~/junk', {state: 'occupied', project: null}],
    ] as const;

    for (const [destination, found] of STATES) {
      test(`says ${found.state} for ${destination}, and Create agrees`, async () => {
        const {main} = world();
        expect(await ask(main, destination)).toEqual({kind: 'offered', value: found});
        const creatable = found.state === 'absent' || found.state === 'empty';
        const made = (await create(main, destination)) as ExampleCreation;
        expect(made.kind).toBe(creatable ? 'created' : 'taken');
        if (made.kind === 'created') {
          expect(made.created.project).toBe(destination);
          // What was made is the example's project from then on.
          expect(await ask(main, destination)).toMatchObject({
            value: {state: 'example_project'},
          });
        }
      });
    }

    test('says it cannot, without asking, to a helper without example_destination', async () => {
      const {link, main} = world();
      link.helper.features.delete('example_destination');
      expect(await ask(main, '~/absent')).toEqual({kind: 'unsupported'});
      expect(link.helper.requests.some(request => request.type === 'examples.destination')).toBe(
        false,
      );
    });

    test('a failed creation names no destination', async () => {
      const {main} = world();
      const failure = await main
        .answer({kind: 'create-from-example', host: LOCAL, example: 'no/such', destination: '~/x'})
        .then(
          () => null,
          (error: Error) => error,
        );
      expect(failure?.message).toStartWith('Could not create the project');
      expect(failure?.message).not.toContain('~/x');
    });
  });
});
