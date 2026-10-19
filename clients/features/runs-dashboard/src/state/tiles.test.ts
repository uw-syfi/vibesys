import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {
  type Attention,
  attentionOf,
  BASE,
  chartOf,
  GROUP_BYS,
  type GroupBy,
  groupTiles,
  type IdeaInput,
  type Mark,
  NEEDS_YOU,
  NOTE_MAX,
  needsYou,
  onMachine,
  type RunInput,
  type StatusKind,
  type Tile,
  tileOf,
  tilesOf,
  UNKNOWN_GROUP,
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
    hardware: null,
    agent: null,
    status: {kind: 'running', word: 'running'},
    problem: null,
    reason: null,
    ideas: null,
    costUsd: null,
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
    agent: random.pick(['Codex', 'Claude', null]),
    status: {kind: random.pick(STATUSES), word: 'word'},
    createdAt: random.int(5),
    points: random.bool() ? ideas(random) : null,
    pointsOmitted: random.int(3),
    attention: random.pick(ATTENTIONS),
    failedStreak: random.int(6),
    silentSeconds: random.bool() ? null : random.int(20_000),
  });
}

function expectNeedsYouFirst(tiles: readonly Tile[], by: GroupBy): void {
  const needing = needsYou(tiles).map(tile => tile.id);
  const groups = groupTiles(tiles, by);
  const first = needing.length === 0 ? undefined : groups[0];
  const others = needing.length === 0 ? groups : groups.slice(1);
  expect(first?.key).toBe(needing.length === 0 ? undefined : NEEDS_YOU);
  expect(first?.tiles.map(tile => tile.id) ?? []).toEqual(needing);
  expect(others.flatMap(group => group.tiles).some(tile => tile.attention)).toBe(false);
  expect(others.some(group => group.key === NEEDS_YOU)).toBe(false);
}

describe('narrowing to a machine', () => {
  test('keeps exactly the runs on it, in order, and no machine keeps them all', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const tiles = tilesOf(Array.from({length: random.int(30)}, (_, at) => randomRun(random, at)));
      expect(onMachine(tiles, null).map(tile => tile.id)).toEqual(tiles.map(tile => tile.id));
      for (const machine of ['host-a', 'host-b']) {
        expect(onMachine(tiles, machine)).toEqual(tiles.filter(tile => tile.machine === machine));
      }
      expect(onMachine(tiles, 'host-z')).toEqual([]);
    }
  });
});

describe('a tile chart', () => {
  test('the best-so-far line never falls and never drops below the base', () => {
    for (const seed of SEEDS) {
      const chart = chartOf(ideas(new Seeded(seed)), 0);
      expect(chart.line.every(value => value >= BASE)).toBe(true);
      expect(chart.line.every((value, at) => at === 0 || value >= (chart.line[at - 1] ?? 0))).toBe(
        true,
      );
    }
  });

  test('plots every idea once, in order, each with its own mark', () => {
    for (const seed of SEEDS) {
      const points = ideas(new Seeded(seed));
      const chart = chartOf(points, 0);
      expect(chart.points).toHaveLength(points.length);
      expect(chart.line).toHaveLength(points.length);
      const marks: Record<Verdict, Mark> = {
        kept: 'kept',
        not_better: 'not-better',
        failed: 'failed',
        running: 'pending',
      };
      expect(chart.points.map(point => point.mark)).toEqual(
        points.map(point => marks[point.verdict]),
      );
    }
  });

  test('a kept idea raises the line to its result; an idea that is not better leaves it', () => {
    const chart = chartOf(
      [
        {ratio: 1.2, verdict: 'kept'},
        {ratio: 1.5, verdict: 'not_better'},
        {ratio: 1.4, verdict: 'kept'},
        {ratio: 1.1, verdict: 'kept'},
      ],
      0,
    );
    expect(chart.line).toEqual([1.2, 1.2, 1.4, 1.4]);
  });

  test('a failed idea sits on the base and one still running sits at the best so far', () => {
    const chart = chartOf(
      [
        {ratio: 1.3, verdict: 'kept'},
        {ratio: null, verdict: 'failed'},
        {ratio: null, verdict: 'running'},
      ],
      0,
    );
    expect(chart.points.map(point => point.y)).toEqual([1.3, BASE, 1.3]);
  });

  test('a run with no ideas has an empty chart', () => {
    expect(chartOf([], 0)).toMatchObject({points: [], line: []});
  });

  test('says how many ideas it stands for, left-out ones included', () => {
    expect(chartOf([{ratio: 1.1, verdict: 'kept'}], 63).label).toContain('64 ideas');
  });
});

describe('a tile', () => {
  test('headlines the best result against the base, green above 1', () => {
    expect(tileOf(run('a', {best: 1.424})).result).toEqual({text: '1.42×', tone: 'ok'});
    expect(tileOf(run('a', {best: 0.9})).result).toEqual({text: '0.90×', tone: 'muted'});
    expect(tileOf(run('a', {best: null, points: []})).result).toBeNull();
  });

  test('falls back to the chart when the digest has no best', () => {
    const tile = tileOf(run('a', {points: [{ratio: 2, verdict: 'kept'}]}));
    expect(tile.result).toEqual({text: '2.00×', tone: 'ok'});
  });

  test('says machine and start time, leaving out what is unknown, under the title it is given', () => {
    expect(tileOf(run('a', {title: 'final'})).facts).toBe('host-a');
    expect(
      tileOf(
        run('a', {title: 'final', hardware: '8 cores', agent: 'Codex', started: 'Oct 19 04:21'}),
      ).facts,
    ).toBe('host-a · Oct 19 04:21');
    expect(tileOf(run('a', {title: 'queue-rs-final2827'})).title).toBe('queue-rs-final2827');
  });

  test('headlines the best measured value, in its unit, when no ratio to the input exists', () => {
    const measured = {value: 287.4, metric: 'output_tokens_per_s', unit: null};
    const tile = tileOf(run('a', {measured, started: 'Oct 19 04:21'}));
    expect(tile.result).toEqual({text: '287', tone: 'muted'});
    expect(tile.facts).toBe('host-a · Oct 19 04:21 · output tokens/s');
    expect(tileOf(run('a', {measured: {...measured, unit: 'tok/s'}})).facts).toBe('host-a · tok/s');
    expect(tileOf(run('a', {measured: {...measured, unit: 'output_tokens_per_s'}})).facts).toBe(
      'host-a · output tokens/s',
    );
    const bare = tileOf(run('a', {measured: {value: 0.5, metric: null, unit: null}}));
    expect([bare.result?.text, bare.facts]).toEqual(['0.5', 'host-a']);
  });

  test('prefers the ratio to the input over the measured value', () => {
    const measured = {value: 287, metric: 'ops_per_s', unit: null};
    expect(tileOf(run('a', {best: 1.5, measured})).result).toEqual({text: '1.50×', tone: 'ok'});
  });

  test('says ideas, cost and time, counting ideas the chart left out', () => {
    const tile = tileOf(
      run('a', {
        costUsd: 4.2,
        seconds: 3900,
        points: [{ratio: 1, verdict: 'kept'}],
        pointsOmitted: 6,
      }),
    );
    expect(tile.figures).toBe('7 ideas · $4.20 · 1h 05m');
    expect(tileOf(run('a')).figures).toBe('0 ideas');
  });

  test('has no chart when the host offers no chart data', () => {
    expect(tileOf(run('a', {points: null})).chart).toBeNull();
  });
});

describe('the tiles of many runs', () => {
  test('are newest first, a total order', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const runs = Array.from({length: random.int(12)}, (_, index) => randomRun(random, index));
      const tiles = tilesOf(runs);
      expect(tiles).toHaveLength(runs.length);
      expect(
        tiles.every((tile, at) => at === 0 || (tiles[at - 1]?.createdAt ?? 0) >= tile.createdAt),
      ).toBe(true);
    }
  });

  test('need you exactly when their attention is not none', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const runs = Array.from({length: random.int(12)}, (_, index) => randomRun(random, index));
      const needing = needsYou(tilesOf(runs)).map(tile => tile.id);
      expect(new Set(needing)).toEqual(
        new Set(runs.filter(each => attentionOf(each) !== 'none').map(each => each.id)),
      );
    }
  });

  test('every tile is in exactly one group, whatever the grouping', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const tiles = tilesOf(
        Array.from({length: random.int(14)}, (_, index) => randomRun(random, index)),
      );
      for (const by of GROUP_BYS) {
        const grouped = groupTiles(tiles, by).flatMap(group => group.tiles.map(tile => tile.id));
        expect(grouped.sort()).toEqual(tiles.map(tile => tile.id).sort());
      }
    }
  });

  test('the runs that need you are one group, first, whatever the grouping, and in no other', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const tiles = tilesOf(
        Array.from({length: random.int(14)}, (_, index) => randomRun(random, index)),
      );
      for (const by of GROUP_BYS) expectNeedsYouFirst(tiles, by);
    }
  });

  test('a tile says why it needs you in a word or two, else why its state is unread', () => {
    const note = (over: Partial<RunInput>): string | null => tileOf(run('a', over)).note;
    expect(note({})).toBeNull();
    expect(note({problem: 'unreachable'})).toBe('unreachable');
    expect(note({attention: 'failed', status: {kind: 'stopped', word: 'ended'}})).toBe('failed');
    // The pill already says failed: the status is said once.
    expect(note({attention: 'failed', status: {kind: 'failed', word: 'failed'}})).toBeNull();
    // Whatever words the pill says it in.
    for (const word of ['failed to start', 'Failed', 'failed · 2 ideas']) {
      expect(note({attention: 'failed', status: {kind: 'failed', word}})).toBeNull();
    }
    expect(note({attention: 'failed', status: {kind: 'failed', word: 'ended'}})).toBeNull();
    // A different word is not said by the pill.
    expect(note({attention: 'failed', status: {kind: 'stopped', word: 'ended'}})).toBe('failed');
    expect(note({attention: 'repeated_failure', failedStreak: 3})).toBe('3 failed in a row');
    expect(note({attention: 'stalled', silentSeconds: 1920})).toBe('stalled 32m');
    expect(note({attention: 'stalled', silentSeconds: null})).toBe('stalled');
    expect(
      note({attention: 'failed', problem: 'unreachable', status: {kind: 'stopped', word: 'ended'}}),
    ).toBe('failed');
  });

  test('a run that is no longer working is never stalled or failing its ideas, and its pill is never green', () => {
    for (const status of STATUSES) {
      for (const attention of ATTENTIONS) {
        const tile = tileOf(run('a', {status: {kind: status, word: 'w'}, attention}));
        const needs = status === 'running' ? attention !== 'none' : attention === 'failed';
        expect(tile.attention).toBe(needs);
        if (needs) expect(['err', 'warn']).toContain(tile.status.tone);
      }
    }
  });

  test('a group keeps the tiles newest first', () => {
    const tiles = tilesOf([
      run('old', {createdAt: 1}),
      run('new', {createdAt: 9}),
      run('mid', {createdAt: 5}),
    ]);
    expect(groupTiles(tiles, 'machine')[0]?.tiles.map(tile => tile.id)).toEqual([
      'new',
      'mid',
      'old',
    ]);
  });

  test('status groups come in a fixed order with their dots, and only when they have runs', () => {
    const tiles = tilesOf([
      run('a', {status: {kind: 'finished', word: 'finished'}}),
      run('b', {status: {kind: 'running', word: 'running'}}),
      run('c', {status: {kind: 'failed', word: 'failed'}}),
    ]);
    expect(groupTiles(tiles, 'status').map(group => [group.title, group.tone])).toEqual([
      ['Running', 'live'],
      ['Failed', 'err'],
      ['Finished', 'ok'],
    ]);
  });

  test('other groups are named by what they group and have no dot; an unknown agent is Unknown', () => {
    const tiles = tilesOf([
      run('a', {agent: 'Codex'}),
      run('b', {agent: null}),
      run('c', {machine: 'host-b'}),
    ]);
    expect(groupTiles(tiles, 'agent').map(group => group.title)).toEqual(['Codex', UNKNOWN_GROUP]);
    expect(
      groupTiles(tiles, 'machine').map(group => [group.title, group.tone, group.tiles.length]),
    ).toEqual([
      ['host-a', null, 2],
      ['host-b', null, 1],
    ]);
  });
});

describe('a tile of a run that crashed', () => {
  const crashed = (over: Partial<RunInput> = {}) =>
    run('20261019-071100', {
      status: {kind: 'failed', word: 'failed'},
      task: 'mpmc',
      started: 'Oct 19, 07:11',
      ...over,
    });

  test('says why the run failed, in the first line of what its record says', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const kind = random.pick(['failed', 'stopped'] as const);
      const why = `reason ${random.int(1000)}`;
      const tile = tileOf(
        crashed({status: {kind, word: kind}, reason: `${why}\nsecond line ${random.int(9)}`}),
      );
      expect(tile.note).toBe(why);
    }
  });

  test('says nothing more than its status when no record gives a reason', () => {
    expect(tileOf(crashed()).note).toBeNull();
    expect(tileOf(crashed({reason: '  '})).note).toBeNull();
  });

  test('does not read a running run reason as a failure', () => {
    expect(
      tileOf(crashed({status: {kind: 'running', word: 'running'}, reason: 'x'})).note,
    ).toBeNull();
  });

  test('is titled as given, with the run id as the hint and the start time among its facts', () => {
    const tile = tileOf(crashed({title: 'queue-rs · mpmc'}));
    expect(tile.title).toBe('queue-rs · mpmc');
    expect(tile.hint).toBe('20261019-071100');
    expect(tile.facts).toBe('host-a · Oct 19, 07:11');
  });

  test('words an error by its class, and keeps the whole line for the pointer', () => {
    const line = 'NestedRepositoryError: before the run could begin it found a repository inside';
    const tile = tileOf(crashed({reason: `${line}\ntraceback`}));
    expect(tile.note).toBe('Nested repository error');
    expect(tile.noteFull).toBe(line);
  });

  test('cuts a long reason between words, never inside one, and never past the limit', () => {
    const WORDS = ['agent', 'was', 'interrupted', 'because', 'the', 'machine', 'went', 'away'];
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const reason = Array.from({length: 1 + random.int(14)}, () => random.pick(WORDS)).join(' ');
      const tile = tileOf(crashed({reason}));
      const note = tile.note ?? '';
      const kept = note.endsWith('…') ? note.slice(0, -1) : note;
      expect(reason === kept || reason.startsWith(`${kept} `)).toBe(true);
      expect(note.length).toBeLessThanOrEqual(NOTE_MAX + 1);
      expect(tile.noteFull).toBe(reason);
    }
  });

  test('says the first clause of a reason that names no error class', () => {
    expect(tileOf(crashed({reason: 'Run interrupted. The host lost power'})).note).toBe(
      'Run interrupted',
    );
  });
});
