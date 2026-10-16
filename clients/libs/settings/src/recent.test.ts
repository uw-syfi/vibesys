import {describe, expect, test} from 'bun:test';
import {type RunStart, recentRunKey} from '@vibesys/runs';
import {remember as rememberRecentRun} from '@vibesys/settings';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  EMPTY_RECENT,
  parseRecent,
  type RecentFile,
  remember,
  rememberInstance,
  type StoredRecentRun,
} from './recent.js';

const HOSTS = ['local', 'ssh:gpu-box', 'ssh:me@node-1'];
const RUN_IDS = ['run-1', 'run-2', 'run-3'];
const STARTS: readonly (RunStart | null)[] = [
  null,
  {harness: null, options: {rounds: null, time_limit_s: null, results: null, roles: []}, args: []},
  {
    harness: {provider: 'claude', model: 'claude-opus-4-5'},
    options: {
      rounds: 3,
      time_limit_s: 3600,
      results: 'keep',
      roles: [{role: 'judge', model: 'claude-sonnet-4-5', reasoning_effort: 'high'}],
    },
    args: ['--max-in-flight', '3'],
  },
];

function pickStart(random: Seeded): {start?: RunStart} {
  const start = random.pick(STARTS);
  return start === null ? {} : {start};
}

function arbitraryRun(random: Seeded, attachedAt: number): StoredRecentRun {
  return {
    host: random.pick(HOSTS),
    project: random.pick(['/srv/a', '~/b']),
    task: random.pick([null, 'spsc', 'mpmc']),
    runId: random.pick(RUN_IDS),
    instanceId: random.int(16).toString(16).padStart(12, '0'),
    attachedAt,
    ...pickStart(random),
  };
}

describe('recent runs', () => {
  test('remember keeps one entry per run, newest first, within the limit, keeping a known task', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      let file: RecentFile = EMPTY_RECENT;
      for (let index = 0; index < 12; index += 1) {
        const run = arbitraryRun(random, index);
        const previous = file.runs.find(
          entry =>
            entry.host === run.host && entry.project === run.project && entry.runId === run.runId,
        );
        file = remember(file, run, 4);
        const [first] = file.runs;
        const start = run.start ?? previous?.start;
        expect(first).toEqual({
          ...run,
          task: run.task ?? previous?.task ?? null,
          ...(start === undefined ? {} : {start}),
        });
        const keys = file.runs.map(recentRunKey);
        expect(new Set(keys).size).toBe(keys.length);
        expect(file.runs.length).toBeLessThanOrEqual(4);
      }
      expect(parseRecent(JSON.parse(JSON.stringify(file)))).toEqual(file);
    }
  });

  test('same-host projects may reuse a run id without replacing or contaminating each other', () => {
    const first: StoredRecentRun = {
      host: 'local',
      project: '/projects/a',
      task: 'spsc',
      runId: 'shared',
      instanceId: '00000000000a',
      attachedAt: 1,
    };
    const second: StoredRecentRun = {
      host: 'local',
      project: '/projects/b',
      task: null,
      runId: 'shared',
      instanceId: null,
      attachedAt: 2,
    };

    const remembered = rememberRecentRun(rememberRecentRun(EMPTY_RECENT, first), second);

    expect(remembered.runs).toEqual([second, first]);
  });

  test('parsing rejects unknown keys and unusable values, naming the path', () => {
    const run = {
      host: 'local',
      project: '/p',
      task: null,
      runId: 'run-1',
      instanceId: '0123456789ab',
      attachedAt: 1,
    };
    expect(() => parseRecent({version: 3, runs: [{...run, extra: 1}]})).toThrow(
      'recent.json: runs[0].extra is not a known field',
    );
    expect(() => parseRecent({version: 3, runs: [{...run, runId: null}]})).toThrow('runs[0].runId');
    expect(() => parseRecent({version: 3, runs: [{...run, host: 'ssh:-oProxy'}]})).toThrow(
      'runs[0].host',
    );
    expect(() => parseRecent({version: 3, runs: [{...run, instanceId: 'bad'}]})).toThrow(
      'runs[0].instanceId',
    );
    expect(() => parseRecent({version: 5, runs: []})).toThrow('version');
  });

  test('a start the run was begun with is validated, naming the key', () => {
    const run = {
      host: 'local',
      project: '/p',
      task: 'spsc',
      runId: 'run-1',
      instanceId: null,
      attachedAt: 1,
      start: {harness: null, options: {rounds: 0}, args: []},
    };
    expect(() => parseRecent({version: 4, runs: [run]})).toThrow(
      'runs[0].start: options.rounds must be a positive whole number',
    );
    expect(() =>
      parseRecent({version: 4, runs: [{...run, start: {harness: null, options: {}, extra: 1}}]}),
    ).toThrow('runs[0].start: start.extra is not a known field');
  });

  test('a version 3 file migrates with no recorded start', () => {
    const parsed = parseRecent({
      version: 3,
      runs: [
        {
          host: 'local',
          project: '/p',
          task: null,
          runId: 'run-1',
          instanceId: null,
          attachedAt: 1,
        },
      ],
    });
    expect(parsed.version).toBe(4);
    expect(parsed.runs[0]?.start).toBeUndefined();
  });

  test('a version 1 file keeps the runs it named and drops those saved before their id was known', () => {
    const v1 = {
      project: '/p',
      task: 'spsc',
      instanceId: '0123456789ab',
      attachedAt: 1,
      host: 'local',
    };
    const parsed = parseRecent({
      version: 1,
      runs: [
        {...v1, runId: 'run-1'},
        {...v1, runId: null},
      ],
    });
    expect(parsed).toEqual({
      version: 4,
      runs: [
        {
          host: 'local',
          project: '/p',
          task: 'spsc',
          runId: 'run-1',
          instanceId: '0123456789ab',
          attachedAt: 1,
        },
      ],
    });
  });

  test('version 2 migrates without guessing an instance, then inspection preserves its locator', () => {
    const parsed = parseRecent({
      version: 2,
      runs: [{host: 'local', project: '/p', task: null, runId: 'run-1', attachedAt: 1}],
    });
    expect(parsed.runs[0]?.instanceId).toBeNull();
    expect(parseRecent(JSON.parse(JSON.stringify(parsed)))).toEqual(parsed);

    const located = rememberInstance(parsed, {
      host: 'local',
      project: '/p',
      runId: 'run-1',
      instanceId: '0123456789ab',
    });
    expect(located).toEqual({
      version: 4,
      runs: [
        {
          host: 'local',
          project: '/p',
          task: null,
          runId: 'run-1',
          instanceId: '0123456789ab',
          attachedAt: 1,
        },
      ],
    });
  });
});
