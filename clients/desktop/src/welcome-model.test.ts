import {describe, expect, test} from 'bun:test';
import {parseInstanceRecord} from './instances.js';
import {parseTaskList} from './task-list.js';
import {fakeRecord} from './testing/fake-record.js';
import {type StopView, stopKey} from './stop-run.js';
import {
  filterHosts,
  moveSelection,
  NO_TASKS,
  recentChanged,
  stripControls,
  suggestCheckout,
  suggestProjects,
  type TaskPicker,
  taskChosen,
  tasksAnswered,
  tasksCleared,
  tasksRequested,
} from './welcome-model.js';
import type {ChromeState} from './welcome-protocol.js';

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

describe('filterHosts', () => {
  const aliases = ['gpu-box', 'cluster.example.org', 'GPU-2'];

  test('an empty query lists every alias', () => {
    expect(filterHosts(aliases, '  ')).toEqual(aliases.map(alias => ({alias, typed: false})));
  });

  test('matches case-insensitively and offers a typed destination last', () => {
    expect(filterHosts(aliases, 'gpu')).toEqual([
      {alias: 'gpu-box', typed: false},
      {alias: 'GPU-2', typed: false},
      {alias: 'gpu', typed: true},
    ]);
    expect(filterHosts(aliases, 'me@node-1')).toEqual([{alias: 'me@node-1', typed: true}]);
    expect(filterHosts(aliases, 'gpu-box')).toEqual([{alias: 'gpu-box', typed: false}]);
  });

  test('never offers a destination ssh would read as an option or that has spaces', () => {
    expect(filterHosts(aliases, '-oProxyCommand=x')).toEqual([]);
    expect(filterHosts(aliases, 'a b')).toEqual([]);
  });
});

describe('moveSelection', () => {
  test('stays in range and wraps, from any start', () => {
    const random = generator(42);
    for (let round = 0; round < 1000; round += 1) {
      const length = Math.floor(random() * 6);
      const index = Math.floor(random() * (length + 2)) - 1;
      const delta = random() < 0.5 ? 1 : -1;
      const next = moveSelection(index, delta, length);
      if (length === 0) expect(next).toBe(-1);
      else {
        expect(next).toBeGreaterThanOrEqual(0);
        expect(next).toBeLessThan(length);
      }
    }
    expect(moveSelection(-1, 1, 3)).toBe(0);
    expect(moveSelection(-1, -1, 3)).toBe(2);
    expect(moveSelection(2, 1, 3)).toBe(0);
  });
});

describe('suggestions', () => {
  const record = (id: string, project: string, root: string | null) =>
    parseInstanceRecord({
      ...fakeRecord(id, '/run/s', 1),
      project_root: project,
      ...(root === null ? {} : {vibesys_root: root}),
    });

  test('the checkout comes from the first record that names one', () => {
    expect(suggestCheckout([])).toBeNull();
    expect(
      suggestCheckout([
        record('0123456789ab', '/p', null),
        record('ba9876543210', '/q', '/home/me/src/vibesys'),
      ]),
    ).toBe('/home/me/src/vibesys');
  });

  test('projects: live ones first, then recent ones on this host, each once', () => {
    const recent = [
      {
        host: 'ssh:gpu',
        project: '/q',
        task: null,
        instanceId: 'aaaaaaaaaaaa',
        runId: null,
        attachedAt: 1,
      },
      {
        host: 'ssh:gpu',
        project: '/r',
        task: null,
        instanceId: 'bbbbbbbbbbbb',
        runId: null,
        attachedAt: 2,
      },
      {
        host: 'local',
        project: '/s',
        task: null,
        instanceId: 'cccccccccccc',
        runId: null,
        attachedAt: 3,
      },
    ];
    expect(suggestProjects([record('0123456789ab', '/q', null)], recent, 'ssh:gpu')).toEqual([
      '/q',
      '/r',
    ]);
  });
});

describe('parseTaskList', () => {
  test('reads the tasks in order', () => {
    expect(
      parseTaskList({version: 1, project_root: '/srv/q', tasks: [{name: 'mpmc'}, {name: 'spsc'}]}),
    ).toEqual({projectRoot: '/srv/q', tasks: ['mpmc', 'spsc']});
  });

  test('rejects unknown keys and bad values, naming the path', () => {
    const cases: [unknown, string][] = [
      [{version: 2, project_root: '/q', tasks: []}, 'tasks.version must be 1'],
      [{version: 1, project_root: '/q', tasks: [], extra: 1}, 'tasks.extra is not a known field'],
      [{version: 1, project_root: 'q', tasks: []}, 'tasks.project_root must be an absolute path'],
      [{version: 1, project_root: '/q', tasks: [{name: 'A B'}]}, 'tasks.tasks[0].name'],
      [{version: 1, project_root: '/q', tasks: [{name: 'a', x: 1}]}, 'tasks.tasks[0].x'],
    ];
    for (const [value, message] of cases) expect(() => parseTaskList(value)).toThrow(message);
  });
});

const TASKS = ['mpmc', 'mpsc', 'spsc', 'verus-mpmc-open'];

function pick<T>(random: () => number, items: readonly T[]): T {
  const item = items[Math.floor(random() * items.length)];
  if (item === undefined) throw new Error('pick from an empty list');
  return item;
}

describe('task picker', () => {
  test('regression: a slow earlier list never clears the task the user picked', () => {
    let picker = tasksRequested(NO_TASKS);
    const first = picker.request;
    picker = tasksRequested(picker);
    picker = tasksAnswered(picker, picker.request, {ok: true, tasks: TASKS});
    picker = taskChosen(picker, 'mpsc');
    const after = tasksAnswered(picker, first, {ok: true, tasks: TASKS});
    expect(after).toBe(picker);
    expect(after.chosen).toBe('mpsc');
  });

  /** One random user or host event; answers may come for any request issued so far. */
  function randomStep(random: () => number, picker: TaskPicker, issued: number[]): TaskPicker {
    const roll = random();
    if (roll < 0.25) {
      const next = tasksRequested(picker);
      issued.push(next.request);
      return next;
    }
    if (roll < 0.3) return tasksCleared(picker);
    if (roll < 0.7 && issued.length > 0) {
      const request = pick(random, issued);
      const tasks = TASKS.slice(0, Math.floor(random() * (TASKS.length + 1)));
      const answer =
        random() < 0.2 ? ({ok: false, error: 'no'} as const) : ({ok: true, tasks} as const);
      const next = tasksAnswered(picker, request, answer);
      if (request !== picker.request) expect(next).toBe(picker);
      return next;
    }
    return taskChosen(picker, pick(random, [...TASKS, 'unknown']));
  }

  test('chosen is always a listed task, and only the latest request is ever shown', () => {
    const random = generator(7);
    for (let round = 0; round < 300; round += 1) {
      let picker: TaskPicker = NO_TASKS;
      const issued: number[] = [];
      for (let step = 0; step < 12; step += 1) {
        picker = randomStep(random, picker, issued);
        const listed = picker.list.kind === 'listed' ? picker.list.tasks : [];
        if (picker.chosen !== null) expect(listed).toContain(picker.chosen);
      }
    }
  });

  test('a project with one task has it chosen', () => {
    const picker = tasksRequested(NO_TASKS);
    expect(tasksAnswered(picker, picker.request, {ok: true, tasks: ['spsc']}).chosen).toBe('spsc');
    expect(tasksAnswered(picker, picker.request, {ok: true, tasks: TASKS}).chosen).toBeNull();
  });
});

type Attached = NonNullable<ChromeState['attached']>;

function attachedRun(random: () => number): Attached {
  const ended = random() < 0.3;
  return {
    host: pick(random, ['local', 'ssh:gpu']),
    hostLabel: 'h',
    instanceId: pick(random, [null, '0123456789ab', 'ba9876543210']),
    runId: null,
    project: '/p',
    status: ended ? 'run ended' : pick(random, ['connected', 'cannot connect', 'connecting']),
    detail: '',
    stuck: ended || random() < 0.4,
    ended,
  };
}

function chromeState(random: () => number): ChromeState {
  const attached = random() < 0.2 ? null : attachedRun(random);
  const stops: Record<string, StopView> = {};
  if (attached?.instanceId != null && random() < 0.6) {
    const phase = pick(random, ['idle', 'stopping', 'ended', 'error'] as const);
    stops[stopKey(attached.host, attached.instanceId)] = {
      phase,
      text: phase,
      canForce: phase === 'error' && random() < 0.5,
    };
  }
  return {mode: pick(random, ['welcome', 'run'] as const), attached, stops};
}

describe('stripControls', () => {
  test('regression: an ended run offers Resume and never Retry', () => {
    const random = generator(3);
    for (let round = 0; round < 500; round += 1) {
      const state = chromeState(random);
      const controls = stripControls(state);
      if (controls.resume) {
        expect(controls.retry).toBe(false);
        expect(controls.stop).toBe(false);
        expect(controls.force).toBe(false);
      }
      if (state.attached?.ended === true) expect(controls.resume).toBe(true);
      if (state.attached === null) expect(Object.values(controls).some(Boolean)).toBe(false);
      expect(controls.back).toBe(state.attached !== null && state.mode === 'welcome');
      if (controls.stop) expect(state.attached?.instanceId).not.toBeNull();
    }
  });
});

describe('recentChanged', () => {
  test('regression: attaching to a different run means the recent runs changed', () => {
    const random = generator(11);
    for (let round = 0; round < 500; round += 1) {
      const before = chromeState(random);
      const after = chromeState(random);
      const same =
        after.attached === null ||
        (before.attached !== null &&
          before.attached.host === after.attached.host &&
          before.attached.instanceId === after.attached.instanceId);
      expect(recentChanged(before, after)).toBe(!same);
      expect(recentChanged(after, after)).toBe(false);
    }
  });
});
