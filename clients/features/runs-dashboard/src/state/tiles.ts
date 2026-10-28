/**
 * The Runs dashboard's tiles: a pure fold from what the app knows of each run (`RunInput`) to the
 * view of its tile (`Tile`), and from the tiles to the groups the screen shows. A tile's chart is
 * the best result so far over the ideas, in the order their results landed, against the base (1.0);
 * a run that needs its user (`attention`) says why in a sentence and is in the "Needs you" group,
 * and in no other.
 */
import {
  cardFacts,
  chartLabel,
  endedLine,
  FAILED_WORD,
  failedWhy,
  failingWhy,
  failingWord,
  measuredCaption,
  NEEDS_YOU_TITLE,
  pausedLine,
  quietWhy,
  quietWord,
  RATIO_CAPTION,
  ratioWord,
  reasonSentence,
  STATUS_TITLES,
  statusHeading,
  withProblem,
  workingLine,
} from './runs-words.js';

/** How an idea came out (the run digest's wording). */
export type Verdict = 'kept' | 'not_better' | 'failed' | 'running';

/** Why a run needs its user, or `none`. */
export type Attention = 'none' | 'failed' | 'repeated_failure' | 'stalled';

/** Where a run stands, for grouping by status. */
export type StatusKind = 'running' | 'paused' | 'finished' | 'failed' | 'stopped' | 'unknown';

/** The tones a status or group dot reads in. */
export type Tone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

/** One idea of a run: its result as a multiple of the base (none before it has a reading) and its verdict. */
export interface IdeaInput {
  readonly ratio: number | null;
  readonly verdict: Verdict;
}

/** A run's best reading in its own metric: the value with the metric's id and unit, when named. */
export interface MeasuredBest {
  readonly value: number;
  readonly metric: string | null;
  readonly unit: string | null;
}

/** What the app knows of one run: the tile's facts, none of them formatted. */
export interface RunInput {
  /** The run's identity as the app names it; a tile's press hands it back. */
  readonly id: string;
  /** The run's id on its machine (`20261019-071100`): never a title, kept as the tile's hint. */
  readonly runId: string;
  /** The run's title, as every screen words it (the sidebar's row, the run page's header). */
  readonly title: string;
  /** The task it runs, or what it is of; groups tiles, never titles one. */
  readonly task: string;
  /** When it started, worded; null when unknown. */
  readonly started: string | null;
  readonly machine: string;
  readonly status: {readonly kind: StatusKind; readonly word: string};
  /** Why the run's state cannot be read, in a word or two; null when it can. */
  readonly problem: string | null;
  /** Who paused a paused run, or why an ended run ended, in a line, when its record says; else null. */
  readonly reason: string | null;
  /** The ideas the run tried, when its journal records them. */
  readonly ideas: number | null;
  readonly seconds: number | null;
  /** The adopted result, or the best so far, as a multiple of the base; null when it has none. */
  readonly best: number | null;
  /** The best idea so far as measured, when the host reads it: what a tile says when `best` is null. */
  readonly measured: MeasuredBest | null;
  /** When the run was created, in milliseconds; the newest run is first. */
  readonly createdAt: number;
  /** The ideas in order, or null when the host offers no chart data. */
  readonly points: readonly IdeaInput[] | null;
  /** The ideas the host left out of `points`. */
  readonly pointsOmitted: number;
  readonly attention: Attention;
  /** The ideas at the end of the run that failed, none passing since. */
  readonly failedStreak: number;
  /** How long the journal has been silent, in seconds; null when it has no event. */
  readonly silentSeconds: number | null;
}

/** A tile's chart: the best result so far as steps over the ideas, against the base. */
export interface TileChart {
  /**
   * From `x` on, the best so far is `y`: the base at 0, then one step after each idea (`x` is how
   * many ideas came in), never below the base and never falling.
   */
  readonly steps: readonly {readonly x: number; readonly y: number}[];
  /** The x range the steps are drawn over: before the first idea to after the last. */
  readonly from: number;
  readonly to: number;
  readonly base: number;
  /** For assistive technology. */
  readonly label: string;
}

/** Why a run needs its user, as its card says it. */
export interface NeedsYou {
  /** The pill: `Failed`, `Quiet 32m`, `3 failed in a row`. */
  readonly label: string;
  readonly tone: 'err' | 'warn';
  /** Why, in a sentence. */
  readonly why: string;
  /** Where and when it ran, what it tried, its best and how long it took. */
  readonly facts: string;
  /** Whether the run has ended, so the card offers to run it again. */
  readonly again: boolean;
}

/** One run's tile. */
export interface Tile {
  readonly id: string;
  readonly title: string;
  /** The run id, for the pointer: a title names a run, the id tells it apart. */
  readonly hint: string;
  /** Where and when it ran. */
  readonly facts: string;
  /** The best result and what it is (`best vs input`, a unit); null when it has none. */
  readonly result: {
    readonly text: string;
    readonly tone: 'ok' | 'muted';
    readonly caption: string;
  } | null;
  /** Null when the host offers no chart data, or the input was never measured to chart against. */
  readonly chart: TileChart | null;
  /** Where the run stands, in a line: `3 ideas working · 7 tried`, `Finished · 6 ideas in 40m`. */
  readonly summary: string;
  /** The summary with why an ended run ended, when its record says, for the pointer. */
  readonly summaryHint: string;
  readonly status: {readonly kind: StatusKind; readonly tone: Tone; readonly label: string};
  /** Why the run needs its user (see `attentionOf`); null when it does not. */
  readonly attention: NeedsYou | null;
  readonly createdAt: number;
  readonly machine: string;
  readonly task: string;
}

/** The base every result is measured against. */
export const BASE = 1;

/**
 * The chart of `points`: the best-so-far line starts at the base and rises with each kept idea's
 * result, one step after each idea; the `omitted` ideas the host left out are counted in its label.
 */
export function chartOf(points: readonly IdeaInput[], omitted: number): TileChart {
  let best = BASE;
  const steps = points.map((point, at) => {
    if (point.verdict === 'kept' && point.ratio !== null) best = Math.max(best, point.ratio);
    return {x: at + 1, y: best};
  });
  return {
    steps: [{x: 0, y: BASE}, ...steps],
    from: 0,
    to: points.length + 1,
    base: BASE,
    label: chartLabel(points.length + omitted),
  };
}

/** The tone of a status's dot. */
export const STATUS_TONES: Readonly<Record<StatusKind, Tone>> = {
  running: 'live',
  paused: 'warn',
  finished: 'ok',
  failed: 'err',
  stopped: 'neutral',
  unknown: 'neutral',
};

/** A metric id in words, as the run page words it: `output_tokens_per_s` reads `output tokens/s`. */
function metricName(metric: string): string {
  const parts = metric
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .split(/[\s_-]+/)
    .filter(part => part !== '')
    .map(part => part.toLowerCase());
  const words: string[] = [];
  for (let at = 0; at < parts.length; at += 1) {
    const [word, next] = [parts[at] ?? '', parts[at + 1]];
    const previous = words.at(-1);
    if (word === 'per' && previous !== undefined && next !== undefined) {
      words[words.length - 1] = `${previous}/${next}`;
      at += 1;
    } else {
      words.push(word);
    }
  }
  return words.join(' ');
}

/** A metric value as the run page writes it: grouped, at most three significant decimals. */
function valueWord(value: number): string {
  const abs = Math.abs(value);
  const digits = abs >= 100 ? 0 : abs >= 10 ? 1 : abs >= 1 ? 2 : 3;
  return value.toLocaleString('en-US', {maximumFractionDigits: digits});
}

/** What a reading is measured in: its unit, else its metric's name, as the run page words them; null when it names neither. */
function unitOf(reading: MeasuredBest): string | null {
  const named = reading.unit ?? reading.metric;
  // An id such as `output_tokens_per_s` is words for a person; a unit such as `ops/s` already is.
  return named === null || named === '' ? null : named.includes('_') ? metricName(named) : named;
}

/** The best result as a multiple of the base: the digest's, else the chart's when it beat the base. */
function bestRatio(input: RunInput, chart: TileChart | null): number | null {
  const finalBest = chart?.steps.at(-1)?.y ?? null;
  return input.best ?? (finalBest !== null && finalBest > BASE ? finalBest : null);
}

/** A tile's headline: the best ratio to the input, else the best reading in its unit, else none. */
function headlineOf(input: RunInput, best: number | null): Tile['result'] {
  if (best !== null) {
    return {text: ratioWord(best), tone: best > BASE ? 'ok' : 'muted', caption: RATIO_CAPTION};
  }
  // No ratio to the input (it was not measured): the best reading itself, which beats nothing known.
  return input.measured === null
    ? null
    : {
        text: valueWord(input.measured.value),
        tone: 'muted',
        caption: measuredCaption(unitOf(input.measured)),
      };
}

/**
 * Whether a run needs its user: the host's judgement, except that a run the app reads as no longer
 * working cannot be stalled or failing its ideas (those say a working run is stuck); only a
 * failure can outlive it.
 */
export function attentionOf(input: RunInput): Attention {
  if (input.status.kind === 'running' || input.attention === 'failed') return input.attention;
  return 'none';
}

/** The first line of `text`, trimmed; null when it has none. */
function firstLine(text: string | null): string | null {
  const line = text
    ?.split('\n')
    .find(each => each.trim() !== '')
    ?.trim();
  return line === undefined || line === '' ? null : line;
}

/** Whether the run no longer works: what it did is done, and it can be run again. */
function hasEnded(kind: StatusKind): boolean {
  return kind !== 'running' && kind !== 'paused';
}

/** Why an ended run ended, in the first line of what its record says; null for a working run. */
function endReason(input: RunInput): string | null {
  return hasEnded(input.status.kind) ? firstLine(input.reason) : null;
}

/** Where the run stands, in a line; `ideas` is what it tried, left-out ideas included. */
function summaryOf(input: RunInput, ideas: number | null): string {
  const {kind, word} = input.status;
  const working = (input.points ?? []).filter(point => point.verdict === 'running').length;
  const line =
    kind === 'running'
      ? workingLine(working, ideas)
      : kind === 'paused'
        ? pausedLine(firstLine(input.reason) ?? word, ideas)
        : kind === 'unknown'
          ? endedLine(word, null, null)
          : endedLine(word, ideas, input.seconds);
  return withProblem(line, input.problem);
}

/** Why a run that needs its user does, as its card says it. */
function needsYouOf(
  input: RunInput,
  attention: Exclude<Attention, 'none'>,
  known: {readonly facts: string; readonly ideas: number | null; readonly best: number | null},
): NeedsYou {
  const failed = (input.points ?? []).filter(point => point.verdict === 'failed').length;
  const reason = endReason(input);
  const said =
    attention === 'failed'
      ? {
          label: FAILED_WORD,
          tone: 'err' as const,
          why:
            reason === null ? failedWhy(known.ideas, failed, known.best) : reasonSentence(reason),
        }
      : attention === 'repeated_failure'
        ? {
            label: failingWord(input.failedStreak),
            tone: 'warn' as const,
            why: failingWhy(input.failedStreak),
          }
        : {
            label: quietWord(input.silentSeconds),
            tone: 'warn' as const,
            why: quietWhy(input.silentSeconds),
          };
  return {
    ...said,
    facts: cardFacts({
      facts: known.facts,
      ideas: known.ideas,
      best: known.best === null ? null : ratioWord(known.best),
      // A working run's time so far says less than how long it has been quiet.
      seconds: hasEnded(input.status.kind) ? input.seconds : null,
    }),
    again: hasEnded(input.status.kind),
  };
}

/** The tile of `input`. */
export function tileOf(input: RunInput): Tile {
  const attention = attentionOf(input);
  const chart = input.points === null ? null : chartOf(input.points, input.pointsOmitted);
  const ideas =
    input.ideas ?? (input.points === null ? null : input.points.length + input.pointsOmitted);
  const best = bestRatio(input, chart);
  const facts = [input.machine, input.started]
    .filter((part): part is string => part !== null)
    .join(' · ');
  const summary = summaryOf(input, ideas);
  const reason = endReason(input);
  const needs = attention === 'none' ? null : needsYouOf(input, attention, {facts, ideas, best});
  return {
    id: input.id,
    title: input.title,
    hint: input.runId,
    facts,
    result: headlineOf(input, best),
    // A run whose input was never measured has nothing to chart against it.
    chart: best === null && input.measured !== null ? null : chart,
    summary,
    summaryHint: reason === null ? summary : `${summary}\n${reason}`,
    status: {
      kind: input.status.kind,
      tone: needs === null ? STATUS_TONES[input.status.kind] : needs.tone,
      label: input.status.word,
    },
    attention: needs,
    createdAt: input.createdAt,
    machine: input.machine,
    task: input.task,
  };
}

/** The tiles of `runs`, newest first (ties by id, so the order is total). */
export function tilesOf(runs: readonly RunInput[]): Tile[] {
  return runs
    .map(tileOf)
    .sort((a, b) => b.createdAt - a.createdAt || (a.id < b.id ? 1 : a.id > b.id ? -1 : 0));
}

/** The tiles of the runs that need their user, in the order given. */
export function needsYou(tiles: readonly Tile[]): Tile[] {
  return tiles.filter(tile => tile.attention !== null);
}

/** What the screen can group runs by. */
export type GroupBy = 'status' | 'machine' | 'task';
export const GROUP_BYS: readonly GroupBy[] = ['status', 'machine', 'task'];

/** The tiles of the runs on `machine` (as a tile names it), or all of them when it is null. */
export function onMachine(tiles: readonly Tile[], machine: string | null): Tile[] {
  return machine === null ? [...tiles] : tiles.filter(tile => tile.machine === machine);
}

/** The key of the group of runs that need their user. */
export const NEEDS_YOU = 'needs-you';

/** The key of the one group the other runs make when they are grouped by status. */
export const BY_STATUS = 'by-status';

/** One group of tiles. */
export interface TileGroup {
  readonly key: string;
  readonly title: string;
  /** The count beside the title; null when the title already counts its tiles. */
  readonly count: number | null;
  /** The group's dot; only "Needs you" has one. */
  readonly tone: Tone | null;
  readonly tiles: readonly Tile[];
}

const STATUS_ORDER: readonly StatusKind[] = [
  'running',
  'paused',
  'failed',
  'finished',
  'stopped',
  'unknown',
];

/** The runs grouped by status: one group, in status order, under a heading that counts each status. */
function statusGroups(tiles: readonly Tile[]): TileGroup[] {
  const byStatus = STATUS_ORDER.map(kind => ({
    kind,
    tiles: tiles.filter(tile => tile.status.kind === kind),
  })).filter(each => each.tiles.length > 0);
  if (byStatus.length === 0) return [];
  const title = statusHeading(
    byStatus.map(each => ({title: STATUS_TITLES[each.kind], count: each.tiles.length})),
  );
  return [
    {key: BY_STATUS, title, count: null, tone: null, tiles: byStatus.flatMap(each => each.tiles)},
  ];
}

function keyedGroups(tiles: readonly Tile[], by: Exclude<GroupBy, 'status'>): TileGroup[] {
  const groups = new Map<string, Tile[]>();
  for (const tile of tiles) {
    const key = by === 'machine' ? tile.machine : tile.task;
    groups.set(key, [...(groups.get(key) ?? []), tile]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, mine]) => ({key, title: key, count: mine.length, tone: null, tiles: mine}));
}

/**
 * `tiles` in groups: first "Needs you", the runs that need their user; then the rest. By status the
 * rest is one group in the order running, paused, failed, finished, stopped, unknown, under a
 * heading that counts each status; otherwise one group per machine or task, in name order. Every
 * tile is in exactly one group (a run that needs its user is in "Needs you" and nowhere else),
 * tiles of one status, machine or task keep the order they came in, and no group is empty.
 */
export function groupTiles(tiles: readonly Tile[], by: GroupBy): TileGroup[] {
  const attention = needsYou(tiles);
  const rest = tiles.filter(tile => tile.attention === null);
  const groups = by === 'status' ? statusGroups(rest) : keyedGroups(rest, by);
  if (attention.length === 0) return groups;
  const needing: TileGroup = {
    key: NEEDS_YOU,
    title: NEEDS_YOU_TITLE,
    count: attention.length,
    tone: 'err',
    tiles: attention,
  };
  return [needing, ...groups];
}
