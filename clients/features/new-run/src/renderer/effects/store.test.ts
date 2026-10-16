import {expect, test} from 'bun:test';
import type {HelperFeature} from '@vibesys/contracts/host-helper';
import type {RecentRun, RunRef} from '@vibesys/runs';
import {readiness, rolesView, startBlocked} from '../ui/view.js';
import {createNewRunStore, type NewRunBridge} from './store.js';

const ok = <T>(value: T) => Promise.resolve({ok: true as const, value});

/** The bridge calls New run makes, answering at once from memory, from a helper with `features`. */
function fakeBridge(
  features: readonly string[] = ['run_options', 'preflight', 'examples'],
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
    preflight: () => record('preflight', {kind: 'offered' as const, value: {checks: [], roles}}),
    createFromExample: (_host, _example, destination) => ok({project: destination, tasks: ['a']}),
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
  expect(store.getState().context.form.project).toEqual({source: 'recent', path: '/p'});
  store.dispatch({
    type: 'project',
    project: {source: 'example', example: 'kv', destination: '/kv'},
  });
  expect(store.getState().context.form.project).toEqual({
    source: 'example',
    example: 'kv',
    destination: '/kv',
  });
  store.dispatch({type: 'recent', recent: [recentRun('local', '/q', 3)]});
  expect(store.getState().context.form.machine).toBe('ssh:gpu');
});

test('a started run is handed on, and a fresh form on the same machine follows it', async () => {
  const clock = manualSchedule();
  const started: RunRef[] = [];
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [recentRun('ssh:gpu', '/p', 2)],
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
  store.dispatch({type: 'project', project: {source: 'folder', path: '/p'}});
  await settle();
  expect(startBlocked(store.getState())).toBe('Checking these inputs.');
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

test('a helper older than New run is never asked to check or start, and Start says why', async () => {
  const clock = manualSchedule();
  const asked: string[] = [];
  const {store} = createNewRunStore(fakeBridge(['preflight', 'harnesses'], asked), {
    recent: [recentRun('ssh:chelan4', '/p', 2)],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  clock.runDue();
  await settle();
  const [first] = readiness(store.getState());
  expect(first).toMatchObject({id: 'helper', state: 'fail', label: 'VibeSys on chelan4'});
  expect(first?.detail).toBe(
    'VibeSys on chelan4 is older than this app: update its checkout to start runs from here ' +
      '(it lacks run_options, examples).',
  );
  expect(startBlocked(store.getState())).toBe('Fix the items marked above to start.');
  store.dispatch({type: 'start'});
  await settle();
  expect(asked).toEqual([]);
  expect(store.getState().context.examples?.value).toEqual({kind: 'unsupported'});
});

test('the role table is read from the run: pending, then its roles, from a helper that reports them', async () => {
  const clock = manualSchedule();
  const roles = ['multi-orchestrator', 'multi-judge'];
  const features = ['run_options', 'preflight', 'examples', 'run_roles'];
  const {store} = createNewRunStore(fakeBridge(features, [], roles), {
    recent: [recentRun('ssh:gpu', '/p', 2)],
    schedule: clock.schedule,
    started: () => undefined,
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
  const {store} = createNewRunStore(fakeBridge(), {
    recent: [recentRun('ssh:gpu', '/p', 2)],
    schedule: clock.schedule,
    started: () => undefined,
  });
  await settle();
  clock.runDue();
  await settle();
  expect(rolesView(store.getState().context)).toEqual({kind: 'unsupported'});
  expect(store.getState().context.form.agent?.roles).toEqual([]);
});
