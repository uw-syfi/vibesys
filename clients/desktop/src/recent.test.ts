import {describe, expect, test} from 'bun:test';
import {parseInstanceRecord} from './instances.js';
import {
  EMPTY_RECENT,
  launchedRunId,
  learnedRunId,
  parseRecent,
  type RecentRun,
  recentStatus,
  remember,
} from './recent.js';
import {exactResumeRunId} from './resume-run.js';
import {fakeRecord} from './testing/fake-record.js';

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

const HOSTS = ['local', 'ssh:gpu-box', 'ssh:me@node-1'];
const IDS = ['0123456789ab', 'ba9876543210', 'aaaaaaaaaaaa', 'bbbbbbbbbbbb'];
const RUNS = [null, 'run-1', 'run-2'];

function pick<T>(random: () => number, items: readonly T[]): T {
  return items[Math.floor(random() * items.length)] as T;
}

function arbitraryRun(random: () => number, attachedAt: number): RecentRun {
  return {
    host: pick(random, HOSTS),
    project: pick(random, ['/srv/a', '~/b']),
    task: pick(random, [null, 'spsc', 'mpmc']),
    instanceId: pick(random, IDS),
    runId: pick(random, RUNS),
    attachedAt,
  };
}

function run(overrides: Partial<RecentRun> = {}): RecentRun {
  return {
    host: 'local',
    project: '/srv/queue-rs',
    task: 'spsc',
    instanceId: '0123456789ab',
    runId: 'run-1',
    attachedAt: 1,
    ...overrides,
  };
}

describe('remember', () => {
  test('puts the run first, never lists one run twice, and keeps at most the limit', () => {
    const random = generator(20_261_010);
    let file = EMPTY_RECENT;
    for (let step = 0; step < 500; step += 1) {
      const next = arbitraryRun(random, step);
      file = remember(file, next, 5);
      expect(file.runs[0]).toMatchObject({instanceId: next.instanceId, host: next.host});
      expect(file.runs.length).toBeLessThanOrEqual(5);
      for (const [index, a] of file.runs.entries()) {
        for (const b of file.runs.slice(index + 1)) {
          const same =
            a.host === b.host &&
            (a.instanceId === b.instanceId || (a.runId !== null && a.runId === b.runId));
          expect(same).toBe(false);
        }
      }
      expect(parseRecent(JSON.parse(JSON.stringify(file)))).toEqual(file);
    }
  });

  test('a reattach without a task keeps the task the run was started with', () => {
    const file = remember(remember(EMPTY_RECENT, run()), run({task: null, attachedAt: 2}));
    expect(file.runs).toEqual([run({attachedAt: 2})]);
  });

  test('a resumed run (same run id, new server) replaces its old entry', () => {
    const file = remember(remember(EMPTY_RECENT, run()), run({instanceId: 'ba9876543210'}));
    expect(file.runs.map(entry => entry.instanceId)).toEqual(['ba9876543210']);
  });

  test('regression: resuming a run records it under its run id before its server reports one', () => {
    const resumed = run({
      instanceId: 'ba9876543210',
      task: null,
      runId: launchedRunId({kind: 'resume', run: exactResumeRunId('run-1')}, null),
      attachedAt: 2,
    });
    const file = remember(remember(EMPTY_RECENT, run()), resumed);
    expect(file.runs).toEqual([run({instanceId: 'ba9876543210', attachedAt: 2})]);
  });

  test('a launch names a run only when its server reported one or it resumes a named run', () => {
    for (const reported of RUNS) {
      expect(launchedRunId({kind: 'start'}, reported)).toBe(reported);
      expect(launchedRunId({kind: 'resume', run: exactResumeRunId('run-9')}, reported)).toBe(
        reported ?? 'run-9',
      );
    }
  });

  test('regression: a run that learns its id merges with its earlier entry and keeps that task', () => {
    const resumed = run({instanceId: 'ba9876543210', task: null, runId: null, attachedAt: 2});
    let file = remember(remember(EMPTY_RECENT, run()), resumed);
    file = remember(file, {...resumed, runId: 'run-1', attachedAt: 3});
    expect(file.runs).toEqual([run({instanceId: 'ba9876543210', attachedAt: 3})]);
  });

  test('keeps the 20 most recent runs by default', () => {
    let file = EMPTY_RECENT;
    for (let index = 0; index < 25; index += 1) {
      file = remember(file, run({instanceId: index.toString(16).padStart(12, '0'), runId: null}));
    }
    expect(file.runs.length).toBe(20);
  });
});

describe('parseRecent', () => {
  test('rejects unknown keys and bad values, naming the path', () => {
    const cases: [unknown, string][] = [
      [{version: 2, runs: []}, 'version must be 1'],
      [{version: 1, runs: [], extra: 1}, 'extra is not a known field'],
      [{version: 1, runs: [{...run(), shell: 1}]}, 'runs[0].shell is not a known field'],
      [{version: 1, runs: [run({host: 'gpu'})]}, 'runs[0].host'],
      [{version: 1, runs: [run({instanceId: 'x'})]}, 'runs[0].instanceId must be 12 hex digits'],
      [{version: 1, runs: [run({project: 'rel'})]}, 'runs[0].project must be an absolute path'],
      [{version: 1, runs: [run({task: 'Bad Task'})]}, 'runs[0].task must be null or match'],
      [{version: 1, runs: [run({attachedAt: -1})]}, 'runs[0].attachedAt'],
    ];
    for (const [value, message] of cases) expect(() => parseRecent(value)).toThrow(message);
  });
});

describe('learnedRunId', () => {
  test('regression: an attached run learns its run id from a live record once its server reports it', () => {
    const records = [
      parseInstanceRecord({...fakeRecord('0123456789ab', '/run/a'), run_id: null}),
      parseInstanceRecord({...fakeRecord('ba9876543210', '/run/b'), run_id: 'run-7'}),
    ];
    expect(learnedRunId(null, 'ba9876543210', records)).toBe('run-7');
    // Nothing to learn: not reported yet, already known, not attached by id, or not listed.
    expect(learnedRunId(null, '0123456789ab', records)).toBeNull();
    expect(learnedRunId('run-7', 'ba9876543210', records)).toBeNull();
    expect(learnedRunId(null, null, records)).toBeNull();
    expect(learnedRunId(null, 'aaaaaaaaaaaa', records)).toBeNull();
  });
});

describe('recentStatus', () => {
  const live = (id: string, runId: string | null, project = '/srv/queue-rs') =>
    parseInstanceRecord({
      ...fakeRecord(id, '/run/s', 1),
      run_id: runId,
      project_root: project,
    });

  test('live while its server is listed', () => {
    expect(recentStatus(run(), [live('0123456789ab', null)])).toEqual({
      kind: 'live',
      instanceId: '0123456789ab',
      status: 'serving',
    });
  });

  test('a resumed run is live on its new server', () => {
    expect(recentStatus(run(), [live('ba9876543210', 'run-1')])).toMatchObject({
      kind: 'live',
      instanceId: 'ba9876543210',
    });
    expect(recentStatus(run(), [live('ba9876543210', 'run-1', '/other')])).toEqual({
      kind: 'ended',
    });
  });

  test('ended when no server drives it, unknown when the host could not say', () => {
    expect(recentStatus(run(), [])).toEqual({kind: 'ended'});
    expect(recentStatus(run(), {error: 'the connection to gpu-box is down'})).toEqual({
      kind: 'unknown',
      detail: 'the connection to gpu-box is down',
    });
  });
});
