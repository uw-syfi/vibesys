import {expect, test} from 'bun:test';
import type {Check} from '../../state/index.js';
import {failureWords} from './cause-words.js';

type Cause = NonNullable<Check['cause']>;

/**
 * Raw text a run prints about a failure: it names flags and files, as the run's own messages do.
 * A cause carries it as data; the screen must keep it out of every headline.
 */
const RAW = '--max-rounds is not a flag this search takes; see --help';

/**
 * One cause of every kind. The type is the closed set from the host contract, so a kind added to
 * Python without a line here is a compile error, and the property below covers it.
 */
const CAUSES: Readonly<{[K in Cause['kind']]: Extract<Cause, {kind: K}>}> = {
  backend_unavailable: {
    kind: 'backend_unavailable',
    task: 'mpmc',
    backend: 'cuda',
    declared: ['cuda'],
  },
  project_dirty: {kind: 'project_dirty', project: '/work/queue-rs', paths: ['a.py', 'b.py']},
  task_not_found: {kind: 'task_not_found', task: 'nosuch', available: ['mpmc', 'mpsc']},
  task_unusable: {kind: 'task_unusable', task: 'mpmc', problem: RAW},
  task_needs_provisioning: {kind: 'task_needs_provisioning', task: 'mpmc'},
  task_image_unsupported: {kind: 'task_image_unsupported', task: 'mpmc', problem: RAW},
  sysbox_missing: {kind: 'sysbox_missing', task: 'mpmc', problem: RAW},
  config_invalid: {kind: 'config_invalid', problem: RAW},
  roles_not_declared: {kind: 'roles_not_declared', roles: ['judge']},
  model_not_vetted: {
    kind: 'model_not_vetted',
    provider: 'codex',
    model: 'foo',
    vetted: ['gpt-a', 'gpt-b'],
    role: 'judge',
    fallback: false,
  },
  arguments_rejected: {kind: 'arguments_rejected', problem: RAW},
  arguments_unrecognized: {kind: 'arguments_unrecognized', arguments: ['--max-rounds', '3']},
  option_not_accepted: {kind: 'option_not_accepted', option: 'rounds'},
  github_unavailable: {kind: 'github_unavailable', problem: RAW},
  measurement_target_not_configured: {
    kind: 'measurement_target_not_configured',
    target: {kind: 'slurm', cluster: 'lab'},
  },
  measurement_target_misfit: {
    kind: 'measurement_target_misfit',
    target: {kind: 'modal'},
    backend: 'rocm',
    offered: 'cuda',
  },
};

function failed(cause: Cause | null, id: Check['id'] = 'settings_valid'): Check {
  return {
    id,
    state: 'fail',
    detail: 'the run printed this',
    ...(cause === null ? {} : {cause}),
  };
}

/** Each cause, and each of the variants that change what its words name. */
function everyCause(): Cause[] {
  return [
    ...Object.values(CAUSES),
    {...CAUSES.model_not_vetted, role: null},
    {...CAUSES.model_not_vetted, model: null},
    {...CAUSES.model_not_vetted, role: null, fallback: true},
    {...CAUSES.backend_unavailable, declared: ['cuda', 'rocm']},
    {...CAUSES.task_unusable, task: null},
    {...CAUSES.task_image_unsupported, task: null},
    ...(
      [
        'rounds',
        'time_limit',
        'results',
        'role_model',
        'role_reasoning_effort',
        'role_agent',
      ] as const
    ).map(option => ({kind: 'option_not_accepted' as const, option})),
  ];
}

function appWorded(): Cause[] {
  return everyCause().filter(cause => cause.kind !== 'project_dirty');
}

test('every cause is worded for this screen, and no headline names a flag or the run’s raw text', () => {
  expect(failureWords(failed(CAUSES.project_dirty), 'host-b')).toBeNull();
  for (const cause of appWorded()) {
    const words = failureWords(failed(cause), 'host-b');

    expect(words).not.toBeNull();
    expect(words?.headline.length).toBeGreaterThan(20);
    // A flag is two hyphens and a word; the screen names its controls instead.
    expect(words?.headline).not.toMatch(/(^|\s)--\w/);
    expect(words?.headline).not.toContain(RAW);
    expect(words?.headline).not.toContain('agent.toml');
    expect(words?.headline).not.toContain('Traceback');
  }
});

test('every cause names the control to change or the step to take next', () => {
  const next = /Choose|Fix|Edit|Clear|Reset|Set |Sign in|Commit|move the evaluator|Change/;
  for (const cause of appWorded()) {
    expect(failureWords(failed(cause), 'host-b')?.headline).toMatch(next);
  }
});

test('the run’s raw text is kept apart as a detail, and only for causes that carry it', () => {
  const carrying = new Set([
    'task_unusable',
    'task_image_unsupported',
    'sysbox_missing',
    'config_invalid',
    'arguments_rejected',
    'github_unavailable',
  ]);
  for (const cause of everyCause()) {
    const raw = failureWords(failed(cause), 'host-b')?.raw ?? null;
    expect(raw !== null).toBe(carrying.has(cause.kind) || cause.kind === 'arguments_unrecognized');
  }
  expect(failureWords(failed(CAUSES.arguments_unrecognized), 'm')?.raw).toBe('--max-rounds 3');
});

test('a search without rounds is worded by the Rounds control, not by its flag', () => {
  const words = failureWords(failed(CAUSES.option_not_accepted), 'host-b');

  expect(words?.headline).toBe(
    'This search does not take a number of rounds. Clear Rounds, or change the search in Extra arguments, then check again.',
  );
});

test('causes fixed outside this screen offer Check again, and those fixed by a control do not', () => {
  const recheck = (cause: Cause) => failureWords(failed(cause), 'm')?.recheck;

  expect(failureWords(failed(CAUSES.project_dirty), 'm')).toBeNull();
  expect(recheck(CAUSES.config_invalid)).toBe(true);
  expect(recheck(CAUSES.github_unavailable)).toBe(true);
  expect(recheck(CAUSES.task_not_found)).toBe(false);
  expect(recheck(CAUSES.arguments_rejected)).toBe(false);
  expect(recheck(CAUSES.backend_unavailable)).toBe(false);
});

test('the machine is named where the failure is about it', () => {
  expect(failureWords(failed(CAUSES.backend_unavailable), 'host-b')?.headline).toContain('host-b');
  expect(failureWords(failed(CAUSES.sysbox_missing), 'host-b')?.headline).toContain('host-b');
  expect(failureWords(failed(CAUSES.github_unavailable), 'host-b')?.headline).toContain('host-b');
});

test('a failed setting with no known cause gets a generic headline, and its message as the detail', () => {
  const words = failureWords(failed(null), 'host-b');

  expect(words?.headline).toBe(
    "These settings can't be used to start a run. Check the project, task, agent and Extra arguments, then check again.",
  );
  expect(words?.raw).toBe('the run printed this');
  expect(words?.recheck).toBe(true);
});

test('a cause from a helper newer than this app is worded generically too', () => {
  const unknown = {kind: 'a_cause_added_later', problem: RAW} as unknown as Cause;

  const words = failureWords(failed(unknown), 'host-b');

  expect(words?.headline).toContain("These settings can't be used");
  expect(words?.headline).not.toContain(RAW);
  expect(words?.raw).toBe('the run printed this');
});

test('a failed check that is not about the settings and has no cause keeps its own detail', () => {
  expect(failureWords(failed(null, 'docker_reachable'), 'host-b')).toBeNull();
});
