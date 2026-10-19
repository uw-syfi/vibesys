import {describe, expect, test} from 'bun:test';
import type {ListedRun, UsageWindowEntry} from '@vibesys/hosts';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type FakeProject, FakeUsageBridge, ZERO_USAGE} from './fake-bridge.js';
import {loadUsage} from './load.js';

const SPAN = {since: 1_000, until: 2_000};
const LIVE = new AbortController().signal;

const runs = (count: number): ListedRun[] =>
  Array.from({length: count}, (_, at) => ({
    runId: `run-${String(count - at).padStart(4, '0')}`,
    task: 'spsc',
    createdAt: 1_000_000 - at,
  }));

const spent = (usd: number): UsageWindowEntry => ({
  ...ZERO_USAGE,
  spentUsd: usd,
  turns: 1,
  byRun: [{runId: 'run-0001', task: 'spsc', costUsd: usd, unpricedTurns: 0}],
});

async function loaded(bridge: FakeUsageBridge) {
  const outcome = await loadUsage(bridge, SPAN, LIVE);
  if (outcome.kind !== 'usage') throw new Error('expected figures');
  return outcome.loaded;
}

describe('loadUsage', () => {
  test('asks each project of each machine for its runs, newest first, over the window', async () => {
    const bridge = new FakeUsageBridge({
      projects: [
        {
          host: 'local',
          project: '/work/a',
          runs: runs(3),
          answer: {kind: 'usage', usage: spent(2)},
        },
        {
          host: 'ssh:host-a',
          project: '/srv/b',
          runs: runs(1),
          answer: {kind: 'usage', usage: spent(3)},
        },
      ],
    });
    const result = await loaded(bridge);
    expect(
      result.sources.map(source => [source.host, source.project, source.usage.spentUsd]),
    ).toEqual([
      ['local', '/work/a', 2],
      ['ssh:host-a', '/srv/b', 3],
    ]);
    expect(bridge.asked).toEqual([
      {host: 'local', project: '/work/a', runIds: ['run-0003', 'run-0002', 'run-0001'], ...SPAN},
      {host: 'ssh:host-a', project: '/srv/b', runIds: ['run-0001'], ...SPAN},
    ]);
    expect(result).toMatchObject({unreachable: 0, unsupported: 0});
    expect(result.sources.map(source => source.leftOut)).toEqual([0, 0]);
  });

  test('property: it names at most the helper bound of runs, and counts the rest as left out', async () => {
    for (const seed of SEEDS.slice(0, 40)) {
      const count = new Seeded(seed).int(260);
      const bridge = new FakeUsageBridge({
        projects: [
          {
            host: 'local',
            project: '/p',
            runs: runs(count),
            answer: {kind: 'usage', usage: spent(1)},
          },
        ],
      });
      const result = await loaded(bridge);
      const named = bridge.asked.reduce((total, each) => total + each.runIds.length, 0);
      expect(named).toBeLessThanOrEqual(100);
      expect(named + (result.sources[0]?.leftOut ?? 0)).toBe(count);
      // A project with no run is not asked at all.
      expect(bridge.asked).toHaveLength(count === 0 ? 0 : 1);
    }
  });

  test('a machine, a project or a helper that cannot answer is counted and the rest are read', async () => {
    const projects: FakeProject[] = [
      {host: 'local', project: '/ok', runs: runs(1), answer: {kind: 'usage', usage: spent(5)}},
      {host: 'local', project: '/unlisted', runs: null, answer: null},
      {host: 'local', project: '/broken', runs: runs(1), answer: null},
      {host: 'ssh:host-a', project: '/old', runs: runs(1), answer: {kind: 'unsupported'}},
    ];
    const result = await loaded(new FakeUsageBridge({projects, unavailable: ['ssh:host-b']}));
    expect(result.sources.map(source => source.project)).toEqual(['/ok']);
    expect(result).toMatchObject({unreachable: 3, unsupported: 1});
  });

  test('when every helper predates the question, the page says so', async () => {
    const bridge = new FakeUsageBridge({
      projects: [{host: 'local', project: '/p', runs: runs(1), answer: {kind: 'unsupported'}}],
    });
    expect(await loadUsage(bridge, SPAN, LIVE)).toEqual({kind: 'unsupported'});
  });

  test('when every machine and project that was asked failed, nothing was read and the question fails', async () => {
    const down = new FakeUsageBridge({
      projects: [{host: 'local', project: '/p', runs: null, answer: null}],
      unavailable: ['ssh:host-a'],
    });
    await expect(loadUsage(down, SPAN, LIVE)).rejects.toThrow('nothing could be read');
    const unreachableOnly = new FakeUsageBridge({projects: [], unavailable: ['ssh:host-a']});
    await expect(loadUsage(unreachableOnly, SPAN, LIVE)).rejects.toThrow('nothing could be read');
  });

  test('no project and no machine is an answer of no usage, not a failure', async () => {
    const bridge = new FakeUsageBridge({projects: []});
    expect(await loaded(bridge)).toMatchObject({sources: [], unreachable: 0});
  });

  test('a model is named by the machine catalogue that lists it', async () => {
    const bridge = new FakeUsageBridge({
      projects: [
        {host: 'local', project: '/p', runs: runs(1), answer: {kind: 'usage', usage: spent(1)}},
      ],
      harnesses: {
        local: {
          kind: 'listed',
          harnesses: [
            {
              provider: 'claude',
              model: 'claude-opus-5-5',
              default: true,
              display: {providerName: 'Claude Code', modelName: 'Opus 5.5'},
            },
            {provider: 'codex', model: 'gpt-6.1-luna', default: false},
          ],
        },
      },
    });
    expect((await loaded(bridge)).names).toEqual([
      {
        harness: 'claude',
        model: 'claude-opus-5-5',
        harnessName: 'Claude Code',
        modelName: 'Opus 5.5',
      },
      {harness: 'codex', model: 'gpt-6.1-luna', harnessName: 'codex', modelName: null},
    ]);
  });

  test('the machines that cannot be listed fail the question, naming why', async () => {
    const bridge = new FakeUsageBridge({projects: [], overviewFails: true});
    await expect(loadUsage(bridge, SPAN, LIVE)).rejects.toThrow('could not be listed');
  });

  test('a question nobody wants any more stops before its answers are used', async () => {
    const bridge = new FakeUsageBridge({
      projects: [
        {host: 'local', project: '/p', runs: runs(1), answer: {kind: 'usage', usage: spent(1)}},
      ],
    });
    const stop = new AbortController();
    const release = bridge.holdAnswers();
    const pending = loadUsage(bridge, SPAN, stop.signal);
    stop.abort();
    release();
    await expect(pending).rejects.toThrow();
  });
});
