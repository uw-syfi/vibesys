import {expect, test} from 'bun:test';
import type {Check, HelperFeature} from '@vibesys/contracts/host-helper';
import {REQUIRED_HELPER_FEATURES} from '@vibesys/hosts';
import type {DestinationFound, MeasureTarget, Offered} from '@vibesys/ipc';
import {computeSettings, exampleCatalogue} from '@vibesys/run-fixtures';
import type {RecentRun, RunRef} from '@vibesys/runs';
import type {Draft} from '../../state/index.js';
import {
  backendOf,
  destinationTaken,
  type ReadinessFix,
  readiness,
  readinessStatus,
  rolesView,
  startBlocked,
} from '../ui/view.js';
import {createNewRunStore, type NewRunBridge, type NewRunStoreOptions} from './store.js';

const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});

/** The bridge calls New run makes, answering at once from memory, from a helper with `features`. */
function fakeBridge(
  features: readonly string[] = REQUIRED_HELPER_FEATURES,
  asked: string[] = [],
  roles: readonly string[] | null = null,
): NewRunBridge {
  const record = <T>(name: string, value: T) => {
    asked.push(name);
    return ok(value);
  };
  return {
    describeMachine: () => ok({kind: 'unsupported' as const}),
    harnesses: () =>
      ok({kind: 'listed' as const, harnesses: [{provider: 'claude', model: 'm', default: true}]}),
    examples: () => record('examples', {kind: 'unsupported' as const}),
    tasks: () => ok(['mpmc']),
    taskGoal: () => ok({kind: 'unsupported' as const}),
    preflight: () =>
      record('preflight', {kind: 'offered' as const, value: {checks: [], roles, backend: null}}),
    createFromExample: (_host, _example, destination) =>
      ok({kind: 'created' as const, created: {project: destination, tasks: ['a']}}),
    exampleDestination: () => ok({kind: 'unsupported' as const}),
    copyOut: (_host, _source, destination) => ok({project: destination, tasks: ['a']}),
    startRun: () => record('startRun', {runId: 'run-2', instanceId: '00000000000c'}),
    helperFeatures: () => ok(features as readonly HelperFeature[]),
    complete: () => ok({kind: 'unsupported' as const}),
  };
}

function recentRun(host: string, project: string, attachedAt: number): RecentRun {
  return {
    host,
    hostLabel: host,
    project,
    task: 'mpmc',
    runId: `r${attachedAt}`,
    instanceId: null,
    attachedAt,
  };
}

/** A schedule whose delays run when the test says. */
function manualSchedule() {
  const due: (() => void)[] = [];
  return {
    schedule: (callback: () => void) => {
      due.push(callback);
      return () => undefined;
    },
    runDue: () => {
      for (const callback of due.splice(0)) callback();
    },
  };
}

/** The store, with `run` run again: its machine, project, task and agent are the form's. */
function runAgain(
  bridge: NewRunBridge,
  run: RecentRun,
  options: Partial<NewRunStoreOptions> & {readonly schedule: NewRunStoreOptions['schedule']},
) {
  const created = createNewRunStore(bridge, {
    recent: [run],
    started: () => undefined,
    ...options,
  });
  created.store.dispatch({type: 'again', run});
  return created;
}

/** Answer that the run is of the user's own project, in the folder `path`. */
function chooseFolder(store: ReturnType<typeof createNewRunStore>['store'], path: string): void {
  store.dispatch({type: 'answer', change: {kind: 'from', from: 'project'}});
  store.dispatch({
    type: 'answer',
    change: {kind: 'project', project: {source: 'folder', path}},
  });
}

async function settle(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

test('the form follows the runs opened until the user changes an input', async () => {
  const clock = manualSchedule();
  const started: RunRef[] = [];
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [],
    schedule: clock.schedule,
    started: run => started.push(run),
  });
  expect(store.getState().context.form.machine).toBe('local');
  store.dispatch({type: 'recent', recent: [recentRun('ssh:gpu', '/p', 2)]});
  expect(store.getState().context.form.machine).toBe('ssh:gpu');
  // A new form knows no project: the last run's project is for running that run again.
  expect(store.getState().context.form.project).toBeNull();
  store.dispatch({type: 'answer', change: {kind: 'from', from: 'project'}});
  expect(store.getState().context.answers.from).toBe('project');
  store.dispatch({type: 'recent', recent: [recentRun('local', '/q', 3)]});
  expect(store.getState().context.form.machine).toBe('ssh:gpu');
  expect(store.getState().context.answers.from).toBe('project');
});

test('a started run is handed on, and a fresh form on the same machine follows it', async () => {
  const clock = manualSchedule();
  const started: RunRef[] = [];
  const {store} = runAgain(fakeBridge(), recentRun('ssh:gpu', '/p', 2), {
    schedule: clock.schedule,
    started: run => started.push(run),
  });
  await settle();
  clock.runDue();
  await settle();
  expect(store.getState().matches({editing: {checks: 'ready'}})).toBe(true);
  store.dispatch({type: 'start'});
  await settle();
  expect(started).toEqual([{host: 'ssh:gpu', runId: 'run-2'}]);
  expect(store.getState().matches('editing')).toBe(true);
  expect(store.getState().context.form.machine).toBe('ssh:gpu');
  expect(store.getState().context.started).toBeNull();
});

test('Start says why it waits: inputs missing, being checked, or an item to fix', async () => {
  const clock = manualSchedule();
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  expect(startBlocked(store.getState())).toBe('Choose a project, task and agent to start.');
  chooseFolder(store, '/p');
  await settle();
  expect(startBlocked(store.getState())).toBe('Checking these inputs.');
  // While the checks run, the one line and its one row do not say the same thing.
  const waiting = readiness(store.getState());
  expect(waiting.map(item => item.id)).toEqual(['checking']);
  const line = readinessStatus(waiting).text;
  expect(line).toBe('Checking');
  expect(waiting.flatMap(item => [item.label, item.detail])).not.toContain(line);
  clock.runDue();
  await settle();
  expect(startBlocked(store.getState())).toBeUndefined();
  store.dispatch({type: 'advanced', advanced: '--model x'});
  expect(readiness(store.getState()).map(item => [item.id, item.state])).toContainEqual([
    'advanced',
    'fail',
  ]);
  expect(startBlocked(store.getState())).toBe('Fix the items marked above to start.');
});

test('a task missing the build its evaluator requires fails Evaluator, fixed by checking again', async () => {
  const clock = manualSchedule();
  const detail =
    'the evaluator requires the candidate build output queue-candidate.so, but the task declares no build';
  const bridge: NewRunBridge = {
    ...fakeBridge(),
    preflight: () =>
      ok({
        kind: 'offered' as const,
        value: {
          checks: [
            {
              id: 'evaluator_available' as const,
              state: 'fail' as const,
              detail,
              fix: {
                kind: 'declare_build' as const,
                manifest: '/p/.vibesys/tasks/mpmc/vibesys.input.toml',
                outputs: ['queue-candidate.so'],
              },
            },
          ],
          roles: null,
          backend: null,
        },
      }),
  };
  const {store} = createNewRunStore(bridge, {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  chooseFolder(store, '/p');
  await settle();
  clock.runDue();
  await settle();

  const evaluator = readiness(store.getState()).find(item => item.id === 'evaluator_available');

  expect(evaluator).toEqual({
    id: 'evaluator_available',
    state: 'fail',
    label: 'Evaluator',
    detail,
    fix: {kind: 'recheck'},
  });
  expect(startBlocked(store.getState())).toBe('Fix the items marked above to start.');
});

test('a helper older than New run is never asked to check or start, and Start says why', async () => {
  const clock = manualSchedule();
  const asked: string[] = [];
  const {store} = runAgain(
    fakeBridge(['preflight', 'harnesses'], asked),
    recentRun('ssh:host-b', '/p', 2),
    {
      schedule: clock.schedule,
    },
  );
  await settle();
  clock.runDue();
  await settle();
  const [first] = readiness(store.getState());
  expect(first).toMatchObject({id: 'helper', state: 'fail', label: 'VibeSys on host-b'});
  expect(first?.detail).toBe('Needs an update');
  expect(startBlocked(store.getState())).toBe('VibeSys on host-b needs an update');
  store.dispatch({type: 'start'});
  await settle();
  expect(asked).toEqual([]);
  expect(store.getState().context.examples?.value).toEqual({kind: 'unsupported'});
});

test('a helper that serves everything New run asks but the run trace needs an update: Start says so and never starts', async () => {
  const clock = manualSchedule();
  const asked: string[] = [];
  const stale = REQUIRED_HELPER_FEATURES.filter(feature => feature !== 'run_trace');
  const {store} = runAgain(fakeBridge(stale, asked), recentRun('ssh:host-b', '/p', 2), {
    schedule: clock.schedule,
  });
  await settle();
  clock.runDue();
  await settle();
  const [first] = readiness(store.getState());
  expect(first).toMatchObject({id: 'helper', state: 'fail', detail: 'Needs an update'});
  expect(startBlocked(store.getState())).toBe('VibeSys on host-b needs an update');
  store.dispatch({type: 'start'});
  await settle();
  expect(asked).not.toContain('startRun');
});

test('a helper with every feature the app needs raises no update item', async () => {
  const clock = manualSchedule();
  const {store} = runAgain(fakeBridge(), recentRun('ssh:host-b', '/p', 2), {
    schedule: clock.schedule,
  });
  await settle();
  clock.runDue();
  await settle();
  expect(readiness(store.getState()).some(item => item.id === 'helper')).toBe(false);
});

test('the role table is read from the run: pending, then its roles, from a helper that reports them', async () => {
  const clock = manualSchedule();
  const roles = ['multi-orchestrator', 'multi-judge'];
  const features = [...REQUIRED_HELPER_FEATURES, 'run_roles'];
  const {store} = runAgain(fakeBridge(features, [], roles), recentRun('ssh:gpu', '/p', 2), {
    schedule: clock.schedule,
  });
  await settle();
  expect(rolesView(store.getState().context)).toEqual({kind: 'pending'});
  clock.runDue();
  await settle();
  expect(rolesView(store.getState().context)).toEqual({kind: 'ready', roles});
  expect(store.getState().context.form.agent?.roles.map(role => role.role)).toEqual(roles);
});

test('a helper that does not report roles gets an empty state, not the roles of another run', async () => {
  const clock = manualSchedule();
  const {store} = runAgain(fakeBridge(), recentRun('ssh:gpu', '/p', 2), {
    schedule: clock.schedule,
  });
  await settle();
  clock.runDue();
  await settle();
  expect(rolesView(store.getState().context)).toEqual({kind: 'unsupported'});
  expect(store.getState().context.form.agent?.roles).toEqual([]);
});

/** A bridge whose preflight reports the backend, failing Run settings with the typed fact. */
function backendBridge(
  backend: 'cpu' | 'cuda',
  failing: boolean,
  features: readonly string[] = [...REQUIRED_HELPER_FEATURES, 'run_backend'],
): NewRunBridge {
  return {
    ...fakeBridge(features),
    preflight: () =>
      ok({
        kind: 'offered' as const,
        value: {
          checks: [
            failing
              ? {
                  id: 'settings_valid' as const,
                  state: 'fail' as const,
                  detail: "task 'mpmc' targets the 'cuda' backend, which this machine cannot run",
                  cause: {
                    kind: 'backend_unavailable' as const,
                    task: 'mpmc',
                    backend,
                    declared: [backend],
                  },
                }
              : {id: 'settings_valid' as const, state: 'ok' as const, detail: 'valid'},
          ],
          roles: null,
          backend,
        },
      }),
  };
}

async function checked(bridge: NewRunBridge) {
  const clock = manualSchedule();
  const {store} = runAgain(bridge, recentRun('ssh:host-b', '/p', 2), {schedule: clock.schedule});
  await settle();
  clock.runDue();
  await settle();
  return store.getState();
}

test('a task for a backend the machine lacks is worded for this screen, naming the task and the machine, never a flag or a setting', async () => {
  const state = await checked(backendBridge('cuda', true));

  const settings = readiness(state).find(item => item.id === 'settings_valid');

  expect(settings?.state).toBe('fail');
  expect(settings?.detail).toBe(
    'The task "mpmc" runs on NVIDIA GPU (CUDA), which host-b does not have. Choose another task or another machine.',
  );
  expect(settings?.detail).not.toMatch(/--|backend:|setting/i);
});

test("the task's backend is shown read-only once the checks report it, and is never a form input", async () => {
  const state = await checked(backendBridge('cpu', false));

  expect(backendOf(state.context)).toBe('cpu');
  expect(Object.keys(state.context.form)).not.toContain('backend');
});

test('a helper without run_backend reports no backend, and the screen shows none', async () => {
  const state = await checked(fakeBridge());

  expect(backendOf(state.context)).toBeNull();
});

test("running a run again keeps that run's model, rounds, time limit, results and extra arguments", async () => {
  const clock = manualSchedule();
  const start = {
    harness: {provider: 'claude', model: 'm2'},
    options: {rounds: 3, time_limit_s: 3600, results: 'publish_github' as const, roles: []},
    agents: {},
    measure_on: {kind: 'same_machine' as const},
    args: ['--name', 'a b'],
  };
  const bridge: NewRunBridge = {
    ...fakeBridge(),
    harnesses: () =>
      ok({
        kind: 'listed' as const,
        harnesses: [
          {provider: 'claude', model: 'm', default: true},
          {provider: 'claude', model: 'm2', default: false},
        ],
      }),
  };
  const {store} = runAgain(
    bridge,
    {...recentRun('ssh:gpu', '/p', 2), start},
    {
      schedule: clock.schedule,
    },
  );
  await settle();
  clock.runDue();
  await settle();

  const {form} = store.getState().context;

  expect(form.agent?.model).toBe('m2');
  expect(form.budget).toEqual({rounds: 3, timeLimitS: 3600, results: 'publish_github'});
  expect(form.advanced).toBe('--name "a b"');
});

test("switching machines carries that machine's last budget and extra arguments, not its project", async () => {
  const clock = manualSchedule();
  const start = {
    harness: null,
    options: {rounds: 5, results: 'keep' as const, roles: []},
    agents: {},
    measure_on: {kind: 'same_machine' as const},
    args: ['--x'],
  };
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [recentRun('local', '/a', 3), {...recentRun('ssh:gpu', '/p', 2), start}],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  store.dispatch({type: 'machine', machine: 'ssh:gpu'});
  await settle();

  const {form} = store.getState().context;

  expect(form.machine).toBe('ssh:gpu');
  expect(form.budget.rounds).toBe(5);
  expect(form.advanced).toBe('--x');
  expect(form.project).toBeNull();
});

/** A bridge whose preflight finds the project's repository dirty, as a helper with project_clean. */
function dirtyBridge(paths: readonly string[]): NewRunBridge {
  return {
    ...fakeBridge(),
    preflight: () =>
      ok({
        kind: 'offered' as const,
        value: {
          checks: [
            {
              id: 'project_clean' as const,
              state: 'fail' as const,
              detail: `${paths.length} uncommitted path(s) in the project`,
              cause: {kind: 'project_dirty' as const, project: '/work/queue-rs', paths: [...paths]},
            },
          ],
          roles: null,
          backend: null,
        },
      }),
  };
}

test('a dirty project is worded for this screen from the typed fact, naming the project and its paths, and offers Check again', async () => {
  const state = await checked(dirtyBridge(['a.py', 'b.py']));

  const item = readiness(state).find(each => each.id === 'project_clean');

  expect(item).toEqual({
    id: 'project_clean',
    state: 'fail',
    label: 'Project changes',
    detail:
      'queue-rs has uncommitted changes (a.py, b.py). Commit or discard them in the project, then check again.',
    fix: {kind: 'recheck'},
  });
  expect(startBlocked(state)).toBe('Fix the items marked above to start.');
});

/** A New run whose project changes under it: `change` edits what the project declares. */
async function changing() {
  const clock = manualSchedule();
  const project = {tasks: ['mpmc'], backend: 'cuda' as 'cuda' | 'cpu'};
  const bridge: NewRunBridge = {
    ...fakeBridge(),
    tasks: () => ok([...project.tasks]),
    preflight: () =>
      ok({kind: 'offered' as const, value: {checks: [], roles: null, backend: project.backend}}),
  };
  const {store} = runAgain(bridge, recentRun('local', '/p', 2), {schedule: clock.schedule});
  const checked = async () => {
    await settle();
    clock.runDue();
    await settle();
    return store.getState().context;
  };
  return {project, store, checked};
}

test("Check again reads the task's declared backend again, without reselecting the task", async () => {
  const {project, store, checked} = await changing();
  expect(backendOf(await checked())).toBe('cuda');

  project.backend = 'cpu';
  store.dispatch({type: 'refresh'});

  expect(backendOf(await checked())).toBe('cpu');
});

test("Check again reads the project's tasks again, keeping the task still offered", async () => {
  const {project, store, checked} = await changing();
  expect((await checked()).tasks?.list).toEqual(['mpmc']);

  project.tasks = ['mpmc', 'mpmc-cpu'];
  store.dispatch({type: 'refresh'});
  const context = await checked();

  expect(context.tasks?.list).toEqual(['mpmc', 'mpmc-cpu']);
  expect(context.form.task).toBe('mpmc');
});

test('Check again clears a task the project no longer has, answering it when the project has only one', async () => {
  const {project, store, checked} = await changing();
  await checked();

  project.tasks = ['mpmc-cpu', 'mpmc-gpu'];
  store.dispatch({type: 'refresh'});
  const cleared = await checked();
  expect(cleared.form.task).toBeNull();
  expect(cleared.answers.task).toBeNull();

  project.tasks = ['mpmc-cpu'];
  store.dispatch({type: 'refresh'});
  const only = await checked();
  expect(only.form.task).toBe('mpmc-cpu');
});

test('a long list of dirty paths is cut with a count of the rest', async () => {
  const paths = ['a', 'b', 'c', 'd', 'e', 'f', 'g'];
  const state = await checked(dirtyBridge(paths));

  const item = readiness(state).find(each => each.id === 'project_clean');

  expect(item?.detail).toContain('(a, b, c, d, e, and 2 more)');
});

/** A bridge whose preflight fails the run's settings for `cause` (or none), as a helper reports it. */
function settingsBridge(
  cause: NonNullable<Check['cause']> | null,
  detail = 'the extra arguments were not accepted',
): NewRunBridge {
  return {
    ...fakeBridge(),
    preflight: () =>
      ok({
        kind: 'offered' as const,
        value: {
          checks: [
            {
              id: 'settings_valid' as const,
              state: 'fail' as const,
              detail,
              ...(cause === null ? {} : {cause}),
            },
          ],
          roles: null,
          backend: null,
        },
      }),
  };
}

test('a search without rounds fails the settings in words that name the Rounds control, never the flag', async () => {
  const state = await checked(
    settingsBridge(
      {kind: 'option_not_accepted', option: 'rounds'},
      'this search does not take the rounds setting',
    ),
  );

  const item = readiness(state).find(each => each.id === 'settings_valid');

  expect(item).toEqual({
    id: 'settings_valid',
    state: 'fail',
    label: 'Run settings',
    detail:
      'This search does not take a number of rounds. Clear Rounds, or change the search in Extra arguments, then check again.',
  });
  expect(startBlocked(state)).toBe('Fix the items marked above to start.');
});

test('rejected extra arguments are worded for Extra arguments, with the parser’s message as a detail', async () => {
  const state = await checked(
    settingsBridge({kind: 'arguments_rejected', problem: 'argument --max-rounds: invalid int'}),
  );

  const item = readiness(state).find(each => each.id === 'settings_valid');

  expect(item?.detail).toBe(
    'The Extra arguments were not accepted. Edit or clear them in Budget and results, then check again.',
  );
  expect(item?.raw).toBe('argument --max-rounds: invalid int');
});

test('a settings failure with no typed cause is generic, and the run’s message is only the detail', async () => {
  const state = await checked(
    settingsBridge(null, '--outer-loop evolve cannot take --max-rounds (see --help)'),
  );

  const item = readiness(state).find(each => each.id === 'settings_valid');

  expect(item?.detail).toBe(
    "These settings can't be used to start a run. Check the project, task, agent and Extra arguments, then check again.",
  );
  expect(item?.raw).toBe('--outer-loop evolve cannot take --max-rounds (see --help)');
  expect(item?.fix).toEqual({kind: 'recheck'});
});

test('a GitHub account the machine lacks is worded by the Results control, and offers Check again', async () => {
  const state = await checked(
    settingsBridge({kind: 'github_unavailable', problem: 'GitHub CLI is not authenticated'}),
  );

  const item = readiness(state).find(each => each.id === 'settings_valid');

  expect(item?.detail).toContain('set Results to keep them there');
  expect(item?.fix).toEqual({kind: 'recheck'});
});

test('More options is remembered per machine: read at the start, written on a choice, kept across a start', async () => {
  const clock = manualSchedule();
  let kept: Readonly<Record<string, boolean>> = {'ssh:gpu': true};
  const writes: Readonly<Record<string, boolean>>[] = [];
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [recentRun('ssh:gpu', '/p', 2)],
    schedule: clock.schedule,
    moreOptions: {
      read: () => kept,
      write: choices => {
        kept = choices;
        writes.push(choices);
      },
    },
    started: () => undefined,
  });
  expect(store.getState().context.moreOpen).toEqual({'ssh:gpu': true});

  store.dispatch({type: 'more_options', machine: 'local', open: true});
  store.dispatch({type: 'more_options', machine: 'ssh:gpu', open: false});

  expect(store.getState().context.moreOpen).toEqual({'ssh:gpu': false, local: true});
  expect(kept).toEqual({'ssh:gpu': false, local: true});
  expect(writes).toHaveLength(2);

  // A choice of the page is not an edit of the run: the form still follows the runs opened.
  store.dispatch({type: 'recent', recent: [recentRun('ssh:other', '/q', 3)]});
  expect(store.getState().context.form.machine).toBe('ssh:other');
  expect(store.getState().context.moreOpen).toEqual({'ssh:gpu': false, local: true});
});

/** A bridge whose `harnesses` is the given call: a missing method, a rejection, or an answer. */
function bridgeWithHarnesses(harnesses: NewRunBridge['harnesses']): NewRunBridge {
  return {...fakeBridge(), harnesses};
}

test('a bridge method that is missing fails its step without showing the raw error', async () => {
  const clock = manualSchedule();
  const missing = (() => {
    throw new TypeError('bridge.harnesses is not a function');
  }) as NewRunBridge['harnesses'];
  const {store} = createNewRunStore(bridgeWithHarnesses(missing), {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();

  const {problem} = store.getState().context;

  expect(problem).toEqual({step: 'harnesses', detail: null, cause: null});
  expect(JSON.stringify(problem)).not.toContain('is not a function');
});

test('a call that rejects fails its step without showing the raw error', async () => {
  const clock = manualSchedule();
  const rejecting: NewRunBridge['harnesses'] = () =>
    Promise.reject(new Error("Error invoking remote method 'harnesses': gone"));
  const {store} = createNewRunStore(bridgeWithHarnesses(rejecting), {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();

  expect(store.getState().context.problem).toEqual({step: 'harnesses', detail: null, cause: null});
});

test('a failure the main process reported keeps its words', async () => {
  const clock = manualSchedule();
  const refusing: NewRunBridge['harnesses'] = () =>
    Promise.resolve({ok: false as const, error: 'host-a is not reachable.', authNeeded: false});
  const {store} = createNewRunStore(bridgeWithHarnesses(refusing), {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();

  expect(store.getState().context.problem).toEqual({
    step: 'harnesses',
    detail: 'host-a is not reachable.',
    cause: null,
  });
});

test('a failure of the connection keeps its typed cause beside the raw words', async () => {
  const clock = manualSchedule();
  const refusing: NewRunBridge['harnesses'] = () =>
    Promise.resolve({
      ok: false as const,
      error: 'the host helper for /srv/x on host-a exited with 1: ModuleNotFoundError',
      authNeeded: false,
      cause: 'incompatible' as const,
    });
  const {store} = createNewRunStore(bridgeWithHarnesses(refusing), {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();

  expect(store.getState().context.problem).toEqual({
    step: 'harnesses',
    detail: 'the host helper for /srv/x on host-a exited with 1: ModuleNotFoundError',
    cause: 'incompatible',
  });
});

test('retry asks the failed step again, and the form it fills in is kept', async () => {
  const clock = manualSchedule();
  let broken = true;
  const flaky: NewRunBridge['harnesses'] = () => {
    if (broken) throw new TypeError('bridge.harnesses is not a function');
    return ok({
      kind: 'listed' as const,
      harnesses: [{provider: 'claude', model: 'm', default: true}],
    });
  };
  const {store} = runAgain(bridgeWithHarnesses(flaky), recentRun('local', '/p', 1), {
    schedule: clock.schedule,
  });
  await settle();
  expect(store.getState().context.harnesses).toBeNull();

  broken = false;
  store.dispatch({type: 'retry'});
  await settle();
  clock.runDue();
  await settle();

  const {context} = store.getState();
  expect(context.problem).toBeNull();
  expect(context.harnesses?.list.map(offer => offer.model)).toEqual(['m']);
  expect(context.form.project).toEqual({source: 'recent', path: '/p'});
  expect(context.form.agent).not.toBeNull();
});

test('a retry does not touch a step that did not fail', async () => {
  const clock = manualSchedule();
  const asked: string[] = [];
  const failing: NewRunBridge = {
    ...fakeBridge(REQUIRED_HELPER_FEATURES, asked),
    tasks: () =>
      Promise.resolve({ok: false as const, error: 'No such project.', authNeeded: false}),
  };
  const {store} = runAgain(failing, recentRun('local', '/p', 1), {schedule: clock.schedule});
  await settle();
  expect(store.getState().context.problem?.step).toBe('tasks');
  const harnessesBefore = store.getState().context.harnesses;

  store.dispatch({type: 'retry'});
  await settle();

  expect(store.getState().context.problem?.step).toBe('tasks');
  expect(store.getState().context.harnesses).toBe(harnessesBefore);
});

/** A draft memory held in memory, with how many times it was written. */
function memory(initial: Draft | null = null) {
  const kept = {draft: initial, writes: 0};
  return {
    kept,
    memory: {
      read: () => kept.draft,
      write: (draft: Draft | null) => {
        kept.draft = draft;
        kept.writes += 1;
      },
    },
  };
}

test('an unfinished form is kept as a draft and found again by the next store of the window', async () => {
  const clock = manualSchedule();
  const first = memory();
  const {store, stop} = createNewRunStore(fakeBridge(), {
    recent: [],
    schedule: clock.schedule,
    draft: first.memory,
    started: () => undefined,
  });
  await settle();
  expect(first.kept.draft).toBeNull();
  chooseFolder(store, '/p');
  store.dispatch({type: 'agent', provider: 'claude', model: 'm'});
  await settle();
  expect(first.kept.draft).toMatchObject({
    machine: 'local',
    answers: {from: 'project', project: {source: 'folder', path: '/p'}, task: 'mpmc'},
    agent: {provider: 'claude', model: 'm'},
  });
  const writes = first.kept.writes;
  stop();

  const again = createNewRunStore(fakeBridge(), {
    recent: [recentRun('ssh:gpu', '/q', 9)],
    schedule: clock.schedule,
    draft: first.memory,
    started: () => undefined,
  });
  await settle();
  const {context} = again.store.getState();
  // The draft wins over the last run: the form is the user's.
  expect(context.form.machine).toBe('local');
  expect(context.answers.project).toEqual({source: 'folder', path: '/p'});
  expect(context.form.task).toBe('mpmc');
  // A restored draft is not written again until it changes.
  expect(first.kept.writes).toBe(writes);
  again.store.dispatch({type: 'recent', recent: [recentRun('ssh:other', '/r', 10)]});
  expect(again.store.getState().context.form.machine).toBe('local');
});

test('starting over, and starting a run, forget the draft', async () => {
  const clock = manualSchedule();
  const kept = memory();
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [recentRun('local', '/p', 2)],
    schedule: clock.schedule,
    draft: kept.memory,
    started: () => undefined,
  });
  chooseFolder(store, '/p');
  expect(kept.kept.draft).not.toBeNull();
  store.dispatch({type: 'reset'});
  expect(kept.kept.draft).toBeNull();

  store.dispatch({type: 'again', run: recentRun('local', '/p', 2)});
  await settle();
  clock.runDue();
  await settle();
  expect(kept.kept.draft?.at).toBe('review');
  store.dispatch({type: 'start'});
  await settle();
  expect(kept.kept.draft).toBeNull();
});

/** The destination of an example as a helper may answer for it, and the fix the Review page offers. */
const DESTINATIONS: readonly {
  readonly name: string;
  readonly answer: Offered<DestinationFound> | 'fails' | 'not_offered';
  readonly fix: ReadinessFix['kind'];
}[] = [
  {
    name: 'nothing',
    answer: {kind: 'offered', value: {state: 'absent', project: null}},
    fix: 'create_from_example',
  },
  {
    name: 'an empty directory',
    answer: {kind: 'offered', value: {state: 'empty', project: null}},
    fix: 'create_from_example',
  },
  {
    name: 'the example project',
    answer: {kind: 'offered', value: {state: 'example_project', project: '/made/stack'}},
    fix: 'use_created',
  },
  {
    name: 'something else',
    answer: {kind: 'offered', value: {state: 'occupied', project: null}},
    fix: 'rename_destination',
  },
  {name: 'a helper that cannot say', answer: {kind: 'unsupported'}, fix: 'create_from_example'},
  {name: 'a question that fails', answer: 'fails', fix: 'create_from_example'},
  {name: 'a helper without the feature', answer: 'not_offered', fix: 'create_from_example'},
];

const FREE = {kind: 'offered' as const, value: {state: 'absent' as const, project: null}};
const TAKEN = {kind: 'offered' as const, value: {state: 'occupied' as const, project: null}};

/** A store whose helper answers `answer` for the destination, with the stack example chosen. */
async function chooseExample(
  answer: (typeof DESTINATIONS)[number]['answer'],
  overrides: Partial<NewRunBridge> = {},
  asked: string[] = [],
) {
  const clock = manualSchedule();
  const features =
    answer === 'not_offered'
      ? [...REQUIRED_HELPER_FEATURES]
      : [...REQUIRED_HELPER_FEATURES, 'example_destination'];
  const bridge: NewRunBridge = {
    ...fakeBridge(features, asked),
    examples: () => ok({kind: 'offered' as const, value: exampleCatalogue}),
    exampleDestination: (_host, _example, destination) => {
      asked.push(`destination ${destination}`);
      if (answer === 'fails') return Promise.reject(new Error('refused'));
      return ok(answer === 'not_offered' ? {kind: 'unsupported' as const} : answer);
    },
    ...overrides,
  };
  const created = createNewRunStore(bridge, {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  for (const change of [
    {kind: 'from', from: 'example'},
    {kind: 'domain', domain: 'data-structures'},
    {kind: 'structure', example: 'data-structures/stack'},
    {kind: 'workload', task: 'spsc'},
  ] as const) {
    created.store.dispatch({type: 'answer', change, advance: true});
  }
  await settle();
  return {...created, clock};
}

const projectItem = (store: ReturnType<typeof createNewRunStore>['store']) =>
  readiness(store.getState()).find(item => item.id === 'project');

for (const {name, answer, fix} of DESTINATIONS) {
  test(`an example whose destination holds ${name} offers ${fix}`, async () => {
    const {store} = await chooseExample(answer);
    const item = projectItem(store);
    expect(item?.fix?.kind).toBe(fix);
    // Taken: the user chooses another name, and cannot create there.
    expect(destinationTaken(store.getState().context)).toBe(fix === 'rename_destination');
    if (fix === 'rename_destination') {
      expect(item?.detail).toBe('~/vibesys-projects/stack is taken');
    }
    if (fix === 'use_created') expect(item?.fix).toMatchObject({path: '/made/stack'});
    expect(store.getState().context.problem).toBeNull();
    expect(startBlocked(store.getState())).toBe('Fix the items marked above to start.');
    // What the screen says never has a home directory in it.
    expect(JSON.stringify(readiness(store.getState()))).not.toMatch(/\/home\/|\/Users\//);
  });
}

test('while the destination is being read, the project item waits and offers no fix', async () => {
  const {store} = await chooseExample(FREE, {
    exampleDestination: () => new Promise(() => undefined),
  });
  const item = projectItem(store);
  expect(item).toMatchObject({state: 'pending', detail: 'Checking the destination'});
  expect(item?.fix).toBeUndefined();
});

test('using the example project that is already there makes it the run project without copying', async () => {
  const asked: string[] = [];
  const copied: string[] = [];
  const {store, clock} = await chooseExample(
    {kind: 'offered', value: {state: 'example_project', project: '/made/stack'}},
    {
      createFromExample: (_host, _example, destination) => {
        copied.push(destination);
        return ok({kind: 'created' as const, created: {project: destination, tasks: ['spsc']}});
      },
    },
    asked,
  );
  const fix = projectItem(store)?.fix;
  if (fix?.kind !== 'use_created') throw new Error('expected the use fix');
  store.dispatch({type: 'use_created', ...fix});
  await settle();
  clock.runDue();
  await settle();
  expect(store.getState().context.form.project).toEqual({
    source: 'created',
    example: 'data-structures/stack',
    destination: '~/vibesys-projects/stack',
    path: '/made/stack',
  });
  expect(copied).toEqual([]);
  expect(asked).toContain('preflight');
  expect(projectItem(store)).toBeUndefined();
});

test('a destination taken when Create runs lands in the same state, not an error', async () => {
  const {store} = await chooseExample(FREE, {
    createFromExample: () => ok({kind: 'taken' as const}),
  });
  const fix = projectItem(store)?.fix;
  if (fix?.kind !== 'create_from_example') throw new Error('expected the create fix');
  store.dispatch({type: 'fix', fix});
  await settle();
  expect(store.getState().context.problem).toBeNull();
  expect(store.getState().context.form.project?.source).toBe('example');
  expect(projectItem(store)?.fix?.kind).toBe('rename_destination');
  expect(destinationTaken(store.getState().context)).toBe(true);
});

test('typing another destination asks again, and a free name offers Create', async () => {
  const {store, clock} = await chooseExample(TAKEN, {
    exampleDestination: (_host, _example, destination) =>
      ok(destination === '~/mine' ? FREE : TAKEN),
  });
  expect(projectItem(store)?.fix?.kind).toBe('rename_destination');
  store.dispatch({type: 'destination', destination: '~/mine'});
  await settle();
  // Until the typing pauses, the name last read is not the one shown as taken.
  expect(projectItem(store)?.state).toBe('pending');
  clock.runDue();
  await settle();
  expect(projectItem(store)?.fix).toMatchObject({
    kind: 'create_from_example',
    destination: '~/mine',
  });
  expect(destinationTaken(store.getState().context)).toBe(false);
});

const MACHINES = {
  cpu: {accelerator: 'cpu' as const, gpus: []},
  rocm: {accelerator: 'rocm' as const, gpus: [{vendor: 'amd', model: 'MI210'}]},
};

/** A store on a machine with `hardware`, the MI210 example chosen and results measured on `target`. */
async function measuredOn(hardware: keyof typeof MACHINES, target: MeasureTarget) {
  const clock = manualSchedule();
  const bridge: NewRunBridge = {
    ...fakeBridge(),
    examples: () => ok({kind: 'offered' as const, value: exampleCatalogue}),
    describeMachine: () =>
      ok({
        kind: 'offered' as const,
        value: {
          checkout: null,
          hardware: {cpu_cores: 64, ...MACHINES[hardware]},
          docker: {reachable: true, version: '27'},
          harness_clis: [],
          runs: {live: 0, ended_today: 0},
        },
      }),
  };
  const {store} = createNewRunStore(bridge, {
    recent: [],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  store.dispatch({
    type: 'compute',
    machine: store.getState().context.form.machine,
    compute: {kind: 'offered', value: computeSettings},
  });
  for (const change of [
    {kind: 'from', from: 'example'},
    {kind: 'domain', domain: 'llm-serving'},
    {kind: 'model', model: 'Qwen3.5 9B'},
    {kind: 'hardware', hardware: 'amd|AMD MI210|1'},
  ] as const) {
    store.dispatch({type: 'answer', change, advance: true});
  }
  store.dispatch({type: 'measure', target});
  await settle();
  return hardwareItem(store);
}

const hardwareItem = (store: ReturnType<typeof createNewRunStore>['store']) =>
  readiness(store.getState()).find(item => item.id === 'hardware_fit');

const MEASURED_ON: readonly (readonly [string, MeasureTarget])[] = [
  ['a cluster', {kind: 'slurm', cluster: 'cluster-m'}],
  ['a cluster that states no hardware', {kind: 'slurm', cluster: 'campus'}],
  ['Modal', {kind: 'modal'}],
];

test('on the same machine, a GPU task on a machine without one is a warning on Review', async () => {
  expect((await measuredOn('cpu', {kind: 'same_machine'}))?.state).toBe('warn');
  expect((await measuredOn('rocm', {kind: 'same_machine'}))?.state).toBe('ok');
});

for (const [name, target] of MEASURED_ON) {
  test(`measured on ${name}, the agent's machine is not judged against the task's GPU`, async () => {
    for (const hardware of ['cpu', 'rocm'] as const) {
      expect(await measuredOn(hardware, target)).toBeUndefined();
    }
  });
}
