import {describe, expect, test} from 'bun:test';
import {Seeded} from '@vibesys/core-state/testing';
import {parseTaskList, taskListProblem} from './task-list.js';

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
