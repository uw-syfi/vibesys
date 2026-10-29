import {describe, expect, test} from 'bun:test';
import {type RunStart, recentRunKey} from '@vibesys/runs';
import {remember as rememberRecentRun} from '@vibesys/settings';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  EMPTY_RECENT,
  parseRecent,
  type RecentFile,
  recentDocument,
  remember,
  rememberInstance,
  type StoredRecentRun,
} from './recent.js';

const HOSTS = ['local', 'ssh:gpu-box', 'ssh:me@node-1'];
const RUN_IDS = ['run-1', 'run-2', 'run-3'];
const STARTS: readonly (RunStart | null)[] = [
  null,
  {
    harness: null,
    options: {rounds: null, time_limit_s: null, results: null, roles: []},
    agents: {},
    measure_on: {kind: 'same_machine'},
    args: [],
  },
  {
    harness: {provider: 'claude', model: 'claude-opus-4-5'},
    options: {
      rounds: 3,
      time_limit_s: 3600,
      results: 'keep',
      roles: [{role: 'judge', model: null, reasoning_effort: 'high'}],
    },
    agents: {judge: {provider: 'claude', model: 'claude-sonnet-4-5'}},
    measure_on: {kind: 'same_machine'},
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
      expect(parseRecent(JSON.parse(JSON.stringify(file)))).toEqual({
        file,
        skipped: [],
        invalid: [],
      });
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

  test('an entry with an unknown key or an unusable value is skipped, naming the key', () => {
    const run = {
      host: 'local',
      project: '/p',
      task: null,
      runId: 'run-1',
      instanceId: '0123456789ab',
      attachedAt: 1,
    };
    const skippedFor = (entry: unknown) =>
      parseRecent({version: 3, runs: [entry]}).skipped.map(each => each.reason);
    expect(skippedFor({...run, extra: 1})).toEqual([
      'recent.json: runs[0].extra is not a known field',
    ]);
    expect(skippedFor({...run, runId: null})).toEqual([
      'recent.json: runs[0].runId must be a run id',
    ]);
    expect(skippedFor({...run, task: ''})).toEqual([
      'recent.json: runs[0].task must be a task name',
    ]);
    expect(skippedFor({...run, host: 'ssh:-oProxy'})[0]).toContain('runs[0].host');
    expect(skippedFor({...run, project: ''})[0]).toContain('runs[0].project');
    expect(skippedFor({...run, instanceId: 'bad'})).toEqual([
      'recent.json: runs[0].instanceId must be a 12-digit instance id',
    ]);
    expect(skippedFor('not an object')).toEqual(['recent.json: runs[0] must be an object']);
  });

  test('the document itself must be valid: its version, keys and list', () => {
    expect(() => parseRecent({version: 5, runs: []})).toThrow('version');
    expect(() => parseRecent({version: 4, runs: {}})).toThrow('runs must be a list');
    expect(() => parseRecent({version: 4, runs: [], extra: 1})).toThrow('extra');
    expect(() => parseRecent([])).toThrow('recent.json must be an object');
  });

  test('any mix of valid and invalid entries keeps exactly the valid ones and reports the others in order', () => {
    const invalids: ((run: StoredRecentRun) => unknown)[] = [
      run => ({...run, task: ''}),
      run => ({...run, instanceId: 'bad'}),
      run => ({...run, runId: 7}),
      run => ({...run, host: 'ssh:'}),
      run => ({...run, attachedAt: -1}),
      run => ({...run, extra: true}),
      () => null,
    ];
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries: unknown[] = [];
      const valid: StoredRecentRun[] = [];
      const invalidAt: number[] = [];
      for (let index = 0; index < 12; index += 1) {
        const run = arbitraryRun(random, index);
        if (random.bool()) {
          valid.push(run);
          entries.push(run);
        } else {
          invalidAt.push(index);
          entries.push(random.pick(invalids)(run));
        }
      }

      const parsed = parseRecent(JSON.parse(JSON.stringify({version: 4, runs: entries})));

      expect(parsed.file).toEqual({version: 4, runs: valid});
      expect(parsed.skipped.map(each => each.index)).toEqual(invalidAt);
      for (const each of parsed.skipped) {
        expect(each.reason).toContain(`recent.json: runs[${each.index}]`);
      }
    }
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
    expect(parsed.file.version).toBe(4);
    expect(parsed.file.runs[0]?.start).toBeUndefined();
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
      file: {
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
      },
      skipped: [],
      invalid: [],
    });
  });

  test('a document with invalid entries keeps each after the same valid entries it followed', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const entries: unknown[] = [];
      for (let index = 0; index < random.int(10); index++) {
        entries.push(random.bool() ? arbitraryRun(random, index) : {bad: index});
      }
      const first = parseRecent({version: 4, runs: entries});
      const document = recentDocument(first.file, first.invalid);
      expect(document.runs).toEqual(entries);
      const again = parseRecent(document);
      expect(again.file).toEqual(first.file);
      expect(again.invalid).toEqual(first.invalid);
    }
  });

  test('version 2 migrates without guessing an instance, then inspection preserves its locator', () => {
    const {file: parsed} = parseRecent({
      version: 2,
      runs: [{host: 'local', project: '/p', task: null, runId: 'run-1', attachedAt: 1}],
    });
    expect(parsed.runs[0]?.instanceId).toBeNull();
    expect(parseRecent(JSON.parse(JSON.stringify(parsed)))).toEqual({
      file: parsed,
      skipped: [],
      invalid: [],
    });

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
