import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/core-state/testing';
import {EMPTY_RECENT, parseRecent, type RecentFile, type RecentRun, remember} from './recent.js';

const HOSTS = ['local', 'ssh:gpu-box', 'ssh:me@node-1'];
const RUN_IDS = ['run-1', 'run-2', 'run-3'];

function arbitraryRun(random: Seeded, attachedAt: number): RecentRun {
  return {
    host: random.pick(HOSTS),
    project: random.pick(['/srv/a', '~/b']),
    task: random.pick([null, 'spsc', 'mpmc']),
    runId: random.pick(RUN_IDS),
    attachedAt,
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
          entry => entry.host === run.host && entry.runId === run.runId,
        );
        file = remember(file, run, 4);
        const [first] = file.runs;
        expect(first).toEqual({...run, task: run.task ?? previous?.task ?? null});
        const keys = file.runs.map(entry => `${entry.host}\n${entry.runId}`);
        expect(new Set(keys).size).toBe(keys.length);
        expect(file.runs.length).toBeLessThanOrEqual(4);
      }
      expect(parseRecent(JSON.parse(JSON.stringify(file)))).toEqual(file);
    }
  });

  test('parsing rejects unknown keys and unusable values, naming the path', () => {
    const run = {host: 'local', project: '/p', task: null, runId: 'run-1', attachedAt: 1};
    expect(() => parseRecent({version: 2, runs: [{...run, extra: 1}]})).toThrow(
      'recent.json: runs[0].extra is not a known field',
    );
    expect(() => parseRecent({version: 2, runs: [{...run, runId: null}]})).toThrow('runs[0].runId');
    expect(() => parseRecent({version: 2, runs: [{...run, host: 'ssh:-oProxy'}]})).toThrow(
      'runs[0].host',
    );
    expect(() => parseRecent({version: 3, runs: []})).toThrow('version');
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
      version: 2,
      runs: [{host: 'local', project: '/p', task: 'spsc', runId: 'run-1', attachedAt: 1}],
    });
  });
});
