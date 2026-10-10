import {describe, expect, test} from 'bun:test';
import {parseInstanceRecord} from './instances.js';
import {parseTaskList} from './task-list.js';
import {fakeRecord} from './testing/fake-record.js';
import {filterHosts, moveSelection, suggestCheckout, suggestProjects} from './welcome-model.js';

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
