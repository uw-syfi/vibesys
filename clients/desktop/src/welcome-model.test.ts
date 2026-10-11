import {describe, expect, test} from 'bun:test';
import {parseInstanceRecord} from './instances.js';
import {parseTaskList, taskListProblem} from './task-list.js';
import {randomChrome} from './testing/fake-chrome.js';
import {fakeRecord} from './testing/fake-record.js';
import {Seeded} from './testing/seeded.js';
import {
  filterHosts,
  moveSelection,
  stripControls,
  suggestCheckout,
  suggestProjects,
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

describe('taskListProblem', () => {
  test('regression: task-list errors are plain language, never a command line or usage dump', () => {
    const cases: [string, string][] = [
      [
        'vibesys tasks: Project root does not exist: /x',
        'Could not read the tasks of /x: Project root does not exist: /x',
      ],
      [
        'usage: vibesys [-h] ...\nvibesys: error: argument command: invalid choice: tasks',
        'The VibeSys checkout on this host is too old to list tasks. Update it, then try again.',
      ],
    ];
    for (const [stderr, expected] of cases) {
      expect(taskListProblem('/x', {kind: 'exited', stderr})).toBe(expected);
    }
    const random = new Seeded(5);
    for (let round = 0; round < 200; round += 1) {
      const stderr = Array.from({length: random.int(4)}, () =>
        random.pick(['usage: vibesys', 'Traceback', 'vibesys tasks --json', 'exited with 2', '']),
      ).join('\n');
      const said = taskListProblem('/p', {kind: 'exited', stderr});
      expect(said).not.toContain('--json');
      expect(said).not.toContain('exited with');
      expect(said).not.toContain('usage:');
    }
  });
});

/** The strip's controls agree with the window state they come from. */
function checkStrip(state: ChromeState): void {
  const controls = stripControls(state);
  if (controls.resume) {
    expect([controls.retry, controls.stop, controls.force]).toEqual([false, false, false]);
  }
  if (state.attached?.link === 'ended') expect(controls.resume).toBe(true);
  expect(controls.retry).toBe(state.attached?.link === 'stuck' && !controls.resume);
  if (state.attached === null) expect(Object.values(controls).some(Boolean)).toBe(false);
  expect(controls.back).toBe(state.attached !== null && state.mode === 'welcome');
  if (controls.stop) expect(state.attached?.instanceId).not.toBeNull();
}

describe('stripControls', () => {
  test('regression: an ended run offers Resume and never Retry', () => {
    const random = new Seeded(3);
    for (let round = 0; round < 500; round += 1) checkStrip(randomChrome(random));
  });
});
