import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type Attention,
  attentionOf,
  BASE,
  BY_STATUS,
  chartOf,
  GROUP_BYS,
  type GroupBy,
  groupTiles,
  type IdeaInput,
  NEEDS_YOU,
  needsYou,
  onMachine,
  type RunInput,
  type StatusKind,
  type Tile,
  tileOf,
  tilesOf,
  type Verdict,
} from './index.js';

const VERDICTS: readonly Verdict[] = ['kept', 'not_better', 'failed', 'running'];
const STATUSES: readonly StatusKind[] = [
  'running',
  'paused',
  'finished',
  'failed',
  'stopped',
  'unknown',
];
const ATTENTIONS: readonly Attention[] = ['none', 'failed', 'repeated_failure', 'stalled'];

function idea(random: Seeded): IdeaInput {
  const verdict = random.pick(VERDICTS);
  const measured = verdict === 'kept' || verdict === 'not_better';
  const ratio = measured || random.bool() ? 0.4 + random.next() * 2.6 : null;
  return {ratio: verdict === 'failed' && random.bool() ? null : ratio, verdict};
}

function ideas(random: Seeded): IdeaInput[] {
  return Array.from({length: random.int(40)}, () => idea(random));
}

function run(id: string, over: Partial<RunInput> = {}): RunInput {
  return {
    id,
    runId: id,
    title: id,
    task: 'mpmc',
    started: null,
    machine: 'host-a',
    status: {kind: 'running', word: 'running'},
    problem: null,
    reason: null,
    ideas: null,
    seconds: null,
    best: null,
    measured: null,
    createdAt: 1,
    points: [],
    pointsOmitted: 0,
    attention: 'none',
    failedStreak: 0,
    silentSeconds: null,
    ...over,
  };
}

function randomRun(random: Seeded, index: number): RunInput {
  return run(`run-${index}`, {
    task: random.pick(['mpmc', 'spsc', 'mpsc']),
    machine: random.pick(['host-a', 'host-b']),
    status: {kind: random.pick(STATUSES), word: random.pick(['running', 'no winner', 'failed'])},
    problem: random.pick([null, null, 'needs an update']),
    reason: random.pick([null, 'paused by you', 'NestedRepositoryError: found a repository\nmore']),
    ideas: random.pick([null, random.int(30)]),
    seconds: random.pick([null, random.int(20_000)]),
    best: random.pick([null, 0.5 + random.next() * 2]),
    createdAt: random.int(5),
    points: random.bool() ? ideas(random) : null,
    pointsOmitted: random.int(3),
    attention: random.pick(ATTENTIONS),
    failedStreak: random.int(6),
    silentSeconds: random.bool() ? null : random.int(20_000),
  });
}

function randomTiles(random: Seeded, most: number): Tile[] {
  return tilesOf(Array.from({length: random.int(most)}, (_, index) => randomRun(random, index)));
}

/** Every text a tile shows a person. */
function shown(tile: Tile): string[] {
  return [
    tile.title,
    tile.facts,
    tile.summary,
    tile.summaryHint,
    ...(tile.result === null ? [] : [tile.result.text, tile.result.caption]),
    ...(tile.attention === null
      ? []
      : [tile.attention.label, tile.attention.why, tile.attention.facts]),
  ];
}

describe('narrowing to a machine', () => {
  test('keeps exactly the runs on it, in order, and no machine keeps them all', () => {
    for (const seed of SEEDS) {
      const tiles = randomTiles(new Seeded(seed), 30);
      expect(onMachine(tiles, null).map(tile => tile.id)).toEqual(tiles.map(tile => tile.id));
      for (const machine of ['host-a', 'host-b']) {
        expect(onMachine(tiles, machine)).toEqual(tiles.filter(tile => tile.machine === machine));
      }
      expect(onMachine(tiles, 'host-z')).toEqual([]);
    }
  });
});

describe('a tile chart', () => {
  test('starts at the base, steps once after each idea, never falls and never drops below the base', () => {
    for (const seed of SEEDS) {
      const points = ideas(new Seeded(seed));
      const chart = chartOf(points, 0);
      expect(chart.steps.map(step => step.x)).toEqual([0, ...points.map((_, at) => at + 1)]);
      expect(chart.steps[0]?.y).toBe(BASE);
      expect([chart.from, chart.to]).toEqual([0, points.length + 1]);
      expect(chart.steps.every(step => step.y >= BASE)).toBe(true);
      expect(
        chart.steps.every((step, at) => at === 0 || step.y >= (chart.steps[at - 1]?.y ?? 0)),
      ).toBe(true);
    }
  });

  test('a kept idea raises the line to its result; an idea that is not better leaves it', () => {
    const chart = chartOf(
      [
        {ratio: 1.2, verdict: 'kept'},
        {ratio: 1.5, verdict: 'not_better'},
        {ratio: null, verdict: 'failed'},
        {ratio: 1.4, verdict: 'kept'},
        {ratio: 1.1, verdict: 'kept'},
        {ratio: null, verdict: 'running'},
      ],
      0,
    );
    expect(chart.steps.map(step => step.y)).toEqual([BASE, 1.2, 1.2, 1.2, 1.4, 1.4, 1.4]);
  });

  test('a run with no ideas stays on the base, and the label counts the ideas left out', () => {
    expect(chartOf([], 0).steps).toEqual([{x: 0, y: BASE}]);
    expect(chartOf([{ratio: 1.1, verdict: 'kept'}], 63).label).toContain('64 ideas');
  });
});

describe('a tile', () => {
  test('headlines the best result against the input, green above 1', () => {
    const caption = 'best vs input';
    expect(tileOf(run('a', {best: 1.424})).result).toEqual({text: '1.42×', tone: 'ok', caption});
    expect(tileOf(run('a', {best: 0.9})).result).toEqual({text: '0.90×', tone: 'muted', caption});
    expect(tileOf(run('a', {best: null, points: []})).result).toBeNull();
    // Ideas that did not beat the input leave the input as the best so far.
    expect(tileOf(run('a', {points: [{ratio: 0.9, verdict: 'not_better'}]})).result).toEqual({
      text: '1.00×',
      tone: 'muted',
      caption,
    });
    // With no best from the digest, the chart's best when it beat the input.
    expect(tileOf(run('a', {points: [{ratio: 2, verdict: 'kept'}]})).result?.text).toBe('2.00×');
  });

  test('headlines the best measured value, captioned with its unit, when no ratio exists', () => {
    const measured = {value: 287.4, metric: 'output_tokens_per_s', unit: null};
    expect(tileOf(run('a', {measured})).result).toEqual({
      text: '287',
      tone: 'muted',
      caption: 'output tokens/s, best',
    });
    expect(tileOf(run('a', {measured: {...measured, unit: 'tok/s'}})).result?.caption).toBe(
      'tok/s, best',
    );
    expect(tileOf(run('a', {measured: {value: 0.5, metric: null, unit: null}})).result).toEqual({
      text: '0.5',
      tone: 'muted',
      caption: 'best',
    });
    expect(tileOf(run('a', {best: 1.5, measured})).result?.text).toBe('1.50×');
    // With no ratio to the input there is nothing to chart against it.
    expect(tileOf(run('a', {measured, points: [{ratio: null, verdict: 'kept'}]})).chart).toBeNull();
    expect(
      tileOf(run('a', {best: 1.5, measured, points: [{ratio: 1.5, verdict: 'kept'}]})).chart,
    ).not.toBeNull();
  });

  test('says where and when it ran, leaving out what is unknown, under the title it is given', () => {
    expect(tileOf(run('a')).facts).toBe('host-a');
    const tile = tileOf(
      run('20261019-071100', {title: 'queue-rs · mpmc', started: 'Oct 19 04:21'}),
    );
    expect([tile.title, tile.hint, tile.facts]).toEqual([
      'queue-rs · mpmc',
      '20261019-071100',
      'host-a · Oct 19 04:21',
    ]);
  });

  test('says in a line how many ideas a working run has working, and how many it tried', () => {
    const working: IdeaInput = {ratio: null, verdict: 'running'};
    const kept: IdeaInput = {ratio: 1.2, verdict: 'kept'};
    const summary = (over: Partial<RunInput>): string => tileOf(run('a', over)).summary;
    expect(summary({points: [kept, working, working], ideas: 7})).toBe('2 ideas working · 7 tried');
    expect(summary({points: [kept, working], pointsOmitted: 3})).toBe('1 idea working · 5 tried');
    expect(summary({points: [kept], ideas: 4})).toBe('Running · 4 ideas tried');
    expect(summary({points: null})).toBe('Running');
  });

  test('says who paused a paused run, and how an ended one ended with what it did in how long', () => {
    const summary = (over: Partial<RunInput>): string => tileOf(run('a', over)).summary;
    const paused = {kind: 'paused', word: 'paused'} as const;
    expect(summary({status: paused, reason: 'paused by you', ideas: 2})).toBe(
      'Paused by you · 2 ideas tried',
    );
    expect(summary({status: paused, ideas: 1})).toBe('Paused · 1 idea tried');
    const finished = {kind: 'finished', word: 'no winner'} as const;
    expect(summary({status: finished, ideas: 6, seconds: 2400})).toBe('No winner · 6 ideas in 40m');
    expect(summary({status: finished, ideas: null, points: null, seconds: 2400})).toBe(
      'No winner · 40m',
    );
    expect(summary({status: {kind: 'stopped', word: 'interrupted'}, ideas: 0})).toBe(
      'Interrupted · 0 ideas',
    );
  });

  test('says why its state cannot be read after its status', () => {
    const tile = tileOf(
      run('a', {status: {kind: 'unknown', word: 'unknown'}, problem: 'needs an update'}),
    );
    expect(tile.summary).toBe('Unknown · needs an update');
  });

  test('keeps why an ended run ended for the pointer, and never a working run’s line', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const kind = random.pick(STATUSES);
      const why = `reason ${random.int(1000)}`;
      const tile = tileOf(run('a', {status: {kind, word: kind}, reason: `${why}\nsecond line`}));
      const ended = kind !== 'running' && kind !== 'paused';
      expect(tile.summaryHint).toBe(ended ? `${tile.summary}\n${why}` : tile.summary);
    }
  });

  test('draws a chart only for a run with an idea, and never without a headline', () => {
    expect(tileOf(run('a', {points: []})).chart).toBeNull();
    for (const seed of SEEDS) {
      for (const tile of randomTiles(new Seeded(seed), 20)) {
        if (tile.chart !== null) expect(tile.result).not.toBeNull();
      }
    }
  });

  test('never shows a hole, an undefined or a NaN, whatever the run', () => {
    for (const seed of SEEDS) {
      for (const tile of randomTiles(new Seeded(seed), 20)) {
        for (const text of shown(tile)) {
          expect(text).not.toMatch(/\bnull\b|\bundefined\b|\bNaN\b|\$\{| {2}|^ | $/);
          expect(text).not.toBe('');
        }
      }
    }
  });
});

describe('a run that needs you', () => {
  const card = (over: Partial<RunInput>) => tileOf(run('a', over)).attention;
  const failed = {kind: 'failed', word: 'failed'} as const;

  test('needs you exactly when the host says so of a working run, or that a run failed', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const runs = Array.from({length: random.int(12)}, (_, index) => randomRun(random, index));
      const needing = needsYou(tilesOf(runs)).map(tile => tile.id);
      expect(new Set(needing)).toEqual(
        new Set(runs.filter(each => attentionOf(each) !== 'none').map(each => each.id)),
      );
    }
    for (const status of STATUSES) {
      for (const attention of ATTENTIONS) {
        const tile = tileOf(run('a', {status: {kind: status, word: 'w'}, attention}));
        const needs = status === 'running' ? attention !== 'none' : attention === 'failed';
        expect(tile.attention !== null).toBe(needs);
        if (needs) expect(['err', 'warn']).toContain(tile.status.tone);
      }
    }
  });

  test('a failed run says why in its record’s words, an error class in words', () => {
    const line = 'NestedRepositoryError: before the run could begin it found a repository inside';
    expect(card({status: failed, attention: 'failed', reason: `${line}\ntraceback`})).toMatchObject(
      {
        label: 'Failed',
        tone: 'err',
        why: 'Nested repository error: before the run could begin it found a repository inside',
      },
    );
    expect(card({status: failed, attention: 'failed', reason: 'the machine went away'})?.why).toBe(
      'The machine went away',
    );
  });

  test('a failed run whose record gives no reason says what its ideas came to', () => {
    const lost: IdeaInput = {ratio: null, verdict: 'failed'};
    const kept: IdeaInput = {ratio: 1.2, verdict: 'kept'};
    const why = (over: Partial<RunInput>): string | undefined =>
      card({status: failed, attention: 'failed', ...over})?.why;
    expect(why({points: [lost, lost, lost]})).toBe(
      'All 3 ideas failed, so nothing beat the input.',
    );
    expect(why({points: [lost]})).toBe('Its one idea failed, so nothing beat the input.');
    expect(why({points: []})).toBe('The run failed before it tried an idea.');
    expect(why({points: [kept, lost]})).toBe('The run failed after 2 ideas; the best was 1.20×.');
    expect(why({points: [lost], pointsOmitted: 2})).toBe('The run failed after 3 ideas.');
    expect(why({points: null})).toBe('The run failed, and its record does not say why.');
    // The digest's count of ideas is another source than the points: it never makes them all fail.
    expect(why({ideas: 2, points: [lost, lost, kept]})).toBe(
      'The run failed after 2 ideas; the best was 1.20×.',
    );
    // A best from the digest is never contradicted by "nothing beat the input".
    expect(why({best: 1.3, points: [lost, lost, lost]})).toBe('All 3 ideas failed.');
  });

  test('says every idea failed only when every point did, and nothing beat the input only with no best', () => {
    const lost: IdeaInput = {ratio: null, verdict: 'failed'};
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      for (let at = 0; at < 20; at += 1) {
        const input = run(`run-${at}`, {
          ...randomRun(random, at),
          status: failed,
          attention: 'failed',
          reason: null,
          points: random.pick([
            null,
            ideas(random),
            Array.from({length: random.int(5)}, () => lost),
          ]),
        });
        const needs = tileOf(input).attention;
        const all = /^(?:All (\d+) ideas|Its one idea) failed/.exec(needs?.why ?? '');
        if (all !== null) {
          const points = input.points ?? [];
          expect(points.every(point => point.verdict === 'failed')).toBe(true);
          expect(input.pointsOmitted).toBe(0);
          expect(points.length).toBe(Number(all[1] ?? 1));
        }
        if (needs?.why.includes('nothing beat the input')) {
          expect(needs.facts).not.toContain('best ');
        }
      }
    }
  });

  test('a working run says how long it has been quiet, or how many ideas failed in a row', () => {
    expect(card({attention: 'stalled', silentSeconds: 1920})).toMatchObject({
      label: 'Quiet 32m',
      tone: 'warn',
      why: 'Still running, but it has recorded nothing for 32m.',
    });
    expect(card({attention: 'stalled'})?.label).toBe('Quiet');
    expect(card({attention: 'repeated_failure', failedStreak: 3})).toMatchObject({
      label: '3 failed in a row',
      why: 'Still running, but its last 3 ideas failed.',
    });
  });

  test('says where it ran, what it tried, its best, how long an ended one took, and offers it again', () => {
    const stalled = card({
      attention: 'stalled',
      started: 'Oct 16 21:00',
      ideas: 2,
      best: 1.3,
      seconds: 600,
    });
    expect(stalled?.facts).toBe('host-a · Oct 16 21:00 · 2 ideas · best 1.30×');
    expect(stalled?.again).toBe(false);
    expect(
      card({status: failed, attention: 'failed', ideas: 3, points: null, seconds: 2400}),
    ).toMatchObject({
      facts: 'host-a · 3 ideas · 40m',
      again: true,
    });
  });
});

function expectNeedsYouFirst(tiles: readonly Tile[], by: GroupBy): void {
  const needing = needsYou(tiles).map(tile => tile.id);
  const groups = groupTiles(tiles, by);
  const first = needing.length === 0 ? undefined : groups[0];
  const others = needing.length === 0 ? groups : groups.slice(1);
  expect(first?.key).toBe(needing.length === 0 ? undefined : NEEDS_YOU);
  expect(first?.tiles.map(tile => tile.id) ?? []).toEqual(needing);
  expect(others.flatMap(group => group.tiles).some(tile => tile.attention !== null)).toBe(false);
}

describe('the tiles of many runs', () => {
  test('are newest first, a total order', () => {
    for (const seed of SEEDS) {
      const tiles = randomTiles(new Seeded(seed), 12);
      expect(
        tiles.every((tile, at) => at === 0 || (tiles[at - 1]?.createdAt ?? 0) >= tile.createdAt),
      ).toBe(true);
    }
  });

  test('every tile is in exactly one group, and no group is empty, whatever the grouping', () => {
    for (const seed of SEEDS) {
      const tiles = randomTiles(new Seeded(seed), 14);
      for (const by of GROUP_BYS) {
        const groups = groupTiles(tiles, by);
        const grouped = groups.flatMap(group => group.tiles.map(tile => tile.id));
        expect(grouped.sort()).toEqual(tiles.map(tile => tile.id).sort());
        expect(groups.every(group => group.tiles.length > 0)).toBe(true);
      }
    }
  });

  test('the runs that need you are one group, first, whatever the grouping, and in no other', () => {
    for (const seed of SEEDS) {
      const tiles = randomTiles(new Seeded(seed), 14);
      for (const by of GROUP_BYS) expectNeedsYouFirst(tiles, by);
    }
  });

  test('by status, the rest are one group in status order, headed by each status and its count', () => {
    for (const seed of SEEDS) {
      const tiles = randomTiles(new Seeded(seed), 20);
      const rest = groupTiles(tiles, 'status').filter(group => group.key !== NEEDS_YOU);
      expect(rest.length).toBeLessThanOrEqual(1);
      const group = rest[0];
      if (group === undefined) continue;
      expect([group.key, group.count, group.tone]).toEqual([BY_STATUS, null, null]);
      const counted = group.title
        .split(' · ')
        .map(part => Number(part.split(' ').at(-1)))
        .reduce((total, each) => total + each, 0);
      expect(counted).toBe(group.tiles.length);
      const kinds = group.tiles.map(tile => tile.status.kind);
      const order = ['running', 'paused', 'failed', 'finished', 'stopped', 'unknown'];
      expect(kinds).toEqual([...kinds].sort((a, b) => order.indexOf(a) - order.indexOf(b)));
    }
  });

  test('the heading names the statuses there are, in order', () => {
    const tiles = tilesOf([
      run('a', {status: {kind: 'finished', word: 'finished'}}),
      run('b', {status: {kind: 'running', word: 'running'}}),
      run('c', {status: {kind: 'finished', word: 'no winner'}}),
      run('d', {status: {kind: 'paused', word: 'paused'}}),
    ]);
    expect(groupTiles(tiles, 'status').map(group => group.title)).toEqual([
      'Running 1 · Paused 1 · Finished 2',
    ]);
  });

  test('by machine or task, one group each, named and counted, newest first', () => {
    const tiles = tilesOf([
      run('old', {createdAt: 1}),
      run('new', {createdAt: 9, task: 'spsc'}),
      run('mid', {createdAt: 5, machine: 'host-b'}),
    ]);
    expect(groupTiles(tiles, 'machine').map(group => [group.title, group.count])).toEqual([
      ['host-a', 2],
      ['host-b', 1],
    ]);
    expect(groupTiles(tiles, 'machine')[0]?.tiles.map(tile => tile.id)).toEqual(['new', 'old']);
    expect(groupTiles(tiles, 'task').map(group => group.title)).toEqual(['mpmc', 'spsc']);
  });
});
