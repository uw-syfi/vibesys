import {describe, expect, test} from 'bun:test';
import {type InstanceListing, parseInstanceRecord} from './instances.js';
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
const STATUS_CONTEXT = {
  hostName: 'gpu-box',
  vibesysCommand: 'uv run --project ~/src/vibesys vibesys',
};

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

function listing(
  records: InstanceListing['records'] = [],
  unverified: InstanceListing['unverified'] = [],
): InstanceListing {
  return {records, unverified};
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
    expect(recentStatus(run(), listing([live('0123456789ab', null)]), STATUS_CONTEXT)).toEqual({
      kind: 'live',
      instanceId: '0123456789ab',
      status: 'serving',
    });
  });

  test('a resumed run is live on its new server', () => {
    expect(
      recentStatus(run(), listing([live('ba9876543210', 'run-1')]), STATUS_CONTEXT),
    ).toMatchObject({
      kind: 'live',
      instanceId: 'ba9876543210',
    });
    expect(
      recentStatus(run(), listing([live('ba9876543210', 'run-1', '/other')]), STATUS_CONTEXT),
    ).toEqual({kind: 'ended'});
  });

  test('ended when no server drives it, unknown when the host could not say', () => {
    expect(recentStatus(run(), listing(), STATUS_CONTEXT)).toEqual({kind: 'ended'});
    expect(
      recentStatus(run(), {error: 'the connection to gpu-box is down'}, STATUS_CONTEXT),
    ).toEqual({kind: 'unknown', detail: 'the connection to gpu-box is down'});
  });

  test('regression: an unverified registry entry stays unknown, never ended', () => {
    expect(recentStatus(run(), listing([], ['0123456789ab']), STATUS_CONTEXT)).toEqual({
      kind: 'unknown',
      detail: 'the host could not confirm whether this run is still running',
    });
  });

  test('regression: a matching incompatible server is unknown, not ended or resumable', () => {
    const incompatible = parseInstanceRecord({...fakeRecord('0123456789ab', '/run/old', 2)});
    const status = recentStatus(run(), listing([incompatible]), STATUS_CONTEXT);
    expect(status.kind).toBe('unknown');
    if (status.kind !== 'unknown') return;
    expect(status.detail).toContain('gpu-box');
    expect(status.detail).toContain(STATUS_CONTEXT.vibesysCommand);
    expect(status.detail).toContain('protocol version 2');
  });

  test('a compatible match wins; only this run being unverified changes its status', () => {
    const random = generator(1_833);
    for (let sample = 0; sample < 300; sample += 1) {
      const current = arbitraryRun(random, sample);
      const otherIds = IDS.filter(id => id !== current.instanceId);
      const unrelated = otherIds.filter(() => random() < 0.5);
      const matchingRecord = live(current.instanceId, current.runId, current.project);
      const incompatible = parseInstanceRecord({
        ...fakeRecord(current.instanceId, '/run/incompatible', 2),
      });
      expect(
        recentStatus(
          current,
          listing([incompatible, matchingRecord], [current.instanceId, ...unrelated]),
          STATUS_CONTEXT,
        ),
      ).toMatchObject({
        kind: 'live',
        instanceId: current.instanceId,
      });
      const unverifiedStatus = recentStatus(
        current,
        listing([incompatible], [current.instanceId, ...unrelated]),
        STATUS_CONTEXT,
      );
      expect(unverifiedStatus.kind).toBe('unknown');
      if (unverifiedStatus.kind === 'unknown') {
        expect(unverifiedStatus.detail).toContain('host could not confirm');
      }
      const incompatibleStatus = recentStatus(
        current,
        listing([incompatible], unrelated),
        STATUS_CONTEXT,
      );
      expect(incompatibleStatus.kind).toBe('unknown');
      if (incompatibleStatus.kind === 'unknown') {
        expect(incompatibleStatus.detail).toContain('protocol version 2');
      }
      expect(recentStatus(current, listing([], unrelated), STATUS_CONTEXT)).toEqual({
        kind: 'ended',
      });
    }
  });
});
