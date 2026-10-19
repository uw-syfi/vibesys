/**
 * The Runs dashboard's tiles: a pure fold from what the app knows of each run (`RunInput`) to the
 * view of its tile (`Tile`), and from the tiles to the groups the screen shows. A tile's chart
 * plots every idea once, in the order the results landed, against the base (1.0), with a
 * best-so-far line that never falls; a run that needs its user (`attention`) says why in a word
 * or two (`note`) and is in the "Needs you" group, and in no other.
 */

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
  /** The hardware it runs on (not on a tile) and the agent that drives it, when known. */
  readonly hardware: string | null;
  readonly agent: string | null;
  readonly status: {readonly kind: StatusKind; readonly word: string};
  /** Why the run's state cannot be read, in a word or two; null when it can. */
  readonly problem: string | null;
  /** Why an ended run ended, in a line, when its record says; null when nothing does. */
  readonly reason: string | null;
  /** The ideas the run tried, when its journal records them. */
  readonly ideas: number | null;
  /** What it has cost, in US dollars, when known. */
  readonly costUsd: number | null;
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

/** The mark of one idea on a tile's chart. */
export type Mark = 'kept' | 'not-better' | 'failed' | 'pending';

/** A tile's chart: one point per idea, and the best-so-far line with a value for each. */
export interface TileChart {
  readonly points: readonly {readonly y: number; readonly mark: Mark}[];
  /** The best result so far after each idea, never below the base and never falling. */
  readonly line: readonly number[];
  readonly base: number;
  /** For assistive technology. */
  readonly label: string;
}

/** One run's tile. */
export interface Tile {
  readonly id: string;
  readonly title: string;
  /** The run id, for the pointer: a title names a run, the id tells it apart. */
  readonly hint: string;
  readonly result: {readonly text: string; readonly tone: 'ok' | 'muted'} | null;
  readonly facts: string;
  /** Null when the host offers no chart data (a chart-less tile). */
  readonly chart: TileChart | null;
  readonly figures: string;
  /**
   * Why the run needs its user, else why its state cannot be read: a few words, cut at a word; or
   * null. `noteFull` is the whole text, for the pointer.
   */
  readonly note: string | null;
  readonly noteFull: string | null;
  readonly status: {readonly kind: StatusKind; readonly tone: Tone; readonly label: string};
  /** Whether the run needs its user (see `attentionOf`). */
  readonly attention: boolean;
  readonly createdAt: number;
  readonly machine: string;
  readonly agent: string | null;
  readonly task: string;
}

/** The base every result is measured against. */
export const BASE = 1;

const MARKS: Readonly<Record<Verdict, Mark>> = {
  kept: 'kept',
  not_better: 'not-better',
  failed: 'failed',
  running: 'pending',
};

/**
 * The chart of `points`: the best-so-far line starts at the base and rises with each kept idea's
 * result. A failed idea has no reading and sits on the base; one still running (no reading yet)
 * sits at the best so far; any other idea sits at its own result, or on the line without one.
 */
export function chartOf(points: readonly IdeaInput[], omitted: number): TileChart {
  let best = BASE;
  const line: number[] = [];
  const plotted = points.map(point => {
    if (point.verdict === 'kept' && point.ratio !== null) best = Math.max(best, point.ratio);
    line.push(best);
    const y = point.verdict === 'failed' ? BASE : (point.ratio ?? best);
    return {y, mark: MARKS[point.verdict]};
  });
  const ideas = points.length + omitted;
  return {
    points: plotted,
    line,
    base: BASE,
    label: `Results of ${ideas} ${ideas === 1 ? 'idea' : 'ideas'} against the base, with the best so far`,
  };
}

/** A multiple of the base with its unit: `1.42×`. */
export function ratioWord(ratio: number): string {
  return `${ratio.toFixed(2)}×`;
}

/** A span in its two largest units: `45s`, `52m`, `1h 05m`. */
export function durationWord(seconds: number): string {
  const whole = Math.max(0, Math.round(seconds));
  if (whole < 60) return `${whole}s`;
  const minutes = Math.floor(whole / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`;
}

function ideasWord(count: number): string {
  return count === 1 ? '1 idea' : `${count} ideas`;
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

/** A tile's headline, and what it is measured in when it is a value and not a multiple of the base. */
function headlineOf(
  input: RunInput,
  chart: TileChart | null,
): {readonly result: Tile['result']; readonly unit: string | null} {
  const finalBest = chart === null ? null : (chart.line.at(-1) ?? null);
  const best = input.best ?? (finalBest !== null && finalBest > BASE ? finalBest : null);
  if (best !== null) {
    return {result: {text: ratioWord(best), tone: best > BASE ? 'ok' : 'muted'}, unit: null};
  }
  // No ratio to the input (it was not measured): the best reading itself, which beats nothing known.
  return input.measured === null
    ? {result: null, unit: null}
    : {
        result: {text: valueWord(input.measured.value), tone: 'muted'},
        unit: unitOf(input.measured),
      };
}

/** The tone of a status pill that says a run needs its user: never the green of a good ending. */
const ATTENTION_TONES: Readonly<Record<Exclude<Attention, 'none'>, Tone>> = {
  failed: 'err',
  repeated_failure: 'warn',
  stalled: 'warn',
};

/**
 * Whether a run needs its user: the host's judgement, except that a run the app reads as no longer
 * working cannot be stalled or failing its ideas (those say a working run is stuck); only a
 * failure can outlive it.
 */
export function attentionOf(input: RunInput): Attention {
  if (input.status.kind === 'running' || input.attention === 'failed') return input.attention;
  return 'none';
}

/** Why a run needs its user, in a word or two (`failed`, `stalled 32m`, `3 failed in a row`). */
export function attentionWord(input: RunInput): string | null {
  switch (input.attention) {
    case 'none':
      return null;
    case 'failed':
      return 'failed';
    case 'repeated_failure':
      return input.failedStreak > 1 ? `${input.failedStreak} failed in a row` : 'failing';
    case 'stalled':
      return input.silentSeconds === null
        ? 'stalled'
        : `stalled ${durationWord(input.silentSeconds)}`;
  }
}

/** The first line of `text`, trimmed; null when it has none. */
function firstLine(text: string | null): string | null {
  const line = text
    ?.split('\n')
    .find(each => each.trim() !== '')
    ?.trim();
  return line === undefined || line === '' ? null : line;
}

/** The longest a tile's note runs, in characters, before it is cut at a word. */
export const NOTE_MAX = 34;

/** `text` cut at the last word boundary within `max` characters, with an ellipsis; whole when it fits. */
function cutAtWord(text: string, max: number): string {
  if (text.length <= max) return text;
  const within = text.slice(0, max + 1);
  const space = within.lastIndexOf(' ');
  // One word longer than the room is kept whole: the tile's own ellipsis is the last resort.
  const cut = space > 0 ? within.slice(0, space) : (text.split(' ')[0] ?? text);
  return `${cut.replace(/[\s,;:.\-–—]+$/, '')}…`;
}

/** `SandboxLifecycleError` as words: `Sandbox lifecycle error`. */
function classWords(name: string): string {
  const words = name.replace(/([a-z0-9])([A-Z])/g, '$1 $2').toLowerCase();
  return `${words.charAt(0).toUpperCase()}${words.slice(1)}`;
}

const ERROR_CLASS = /^([A-Z][A-Za-z0-9]*(?:Error|Exception|Failure|Interrupted))\s*:/;

/**
 * A reason in a few words: an error's class (`NestedRepositoryError: ...` reads `Nested repository
 * error`), else the first clause of the line, cut at a word. The whole line is `full`.
 */
function reasonWords(text: string): {readonly note: string; readonly full: string} {
  const full = text.trim();
  const named = ERROR_CLASS.exec(full);
  const clause = (full.split(/[.;:](?:\s|$)|\s[-–—]\s/)[0] ?? full).trim();
  const short = named?.[1] === undefined ? clause : classWords(named[1]);
  return {note: cutAtWord(short === '' ? full : short, NOTE_MAX), full};
}

/**
 * The words of a failed or stopped run's tile: why it ended, when its record says, else nothing
 * (the status pill already says `failed`).
 */
function endNote(input: RunInput): string | null {
  return input.status.kind === 'failed' || input.status.kind === 'stopped'
    ? firstLine(input.reason)
    : null;
}

/** The note of a tile unless the status pill already says it (a status is said once), with its full text. */
function noteOf(
  note: {readonly note: string; readonly full: string} | null,
  status: string,
): Pick<Tile, 'note' | 'noteFull'> {
  return note === null || note.note.toLowerCase() === status.toLowerCase()
    ? {note: null, noteFull: null}
    : {note: note.note, noteFull: note.full};
}

/** A reason that is already a few words says itself whole. */
function asNote(text: string | null): {readonly note: string; readonly full: string} | null {
  return text === null ? null : {note: text, full: text};
}

/** The tile of `input`. */
export function tileOf(input: RunInput): Tile {
  const attention = attentionOf(input);
  const chart = input.points === null ? null : chartOf(input.points, input.pointsOmitted);
  const ideas =
    input.ideas ?? (input.points === null ? null : input.points.length + input.pointsOmitted);
  const figures = [
    ...(ideas === null ? [] : [ideasWord(ideas)]),
    ...(input.costUsd === null ? [] : [`$${input.costUsd.toFixed(2)}`]),
    ...(input.seconds === null ? [] : [durationWord(input.seconds)]),
  ].join(' · ');
  const ended = endNote(input);
  const {result, unit} = headlineOf(input, chart);
  return {
    id: input.id,
    title: input.title,
    hint: input.runId,
    result,
    facts: [input.machine, input.started, unit]
      .filter((part): part is string => part !== null)
      .join(' · '),
    chart,
    figures,
    ...noteOf(
      ended === null
        ? asNote(attentionWord({...input, attention}) ?? input.problem)
        : reasonWords(ended),
      input.status.word,
    ),
    status: {
      kind: input.status.kind,
      tone: attention === 'none' ? STATUS_TONES[input.status.kind] : ATTENTION_TONES[attention],
      label: input.status.word,
    },
    attention: attention !== 'none',
    createdAt: input.createdAt,
    machine: input.machine,
    agent: input.agent,
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
  return tiles.filter(tile => tile.attention);
}

/** What the screen can group runs by. */
export type GroupBy = 'status' | 'machine' | 'task' | 'agent';
export const GROUP_BYS: readonly GroupBy[] = ['status', 'machine', 'task', 'agent'];

/** The tiles of the runs on `machine` (as a tile names it), or all of them when it is null. */
export function onMachine(tiles: readonly Tile[], machine: string | null): Tile[] {
  return machine === null ? [...tiles] : tiles.filter(tile => tile.machine === machine);
}

/** The key of the group of runs that need their user. */
export const NEEDS_YOU = 'needs-you';

/** One group of tiles, newest first. */
export interface TileGroup {
  readonly key: string;
  readonly title: string;
  /** The status dot of a status group; other groups have none. */
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

/** The word of a group of runs whose agent is not known. */
export const UNKNOWN_GROUP = 'Unknown';

const STATUS_TITLES: Readonly<Record<StatusKind, string>> = {
  running: 'Running',
  paused: 'Paused',
  failed: 'Failed',
  finished: 'Finished',
  stopped: 'Stopped',
  unknown: 'Unknown',
};

function statusGroups(tiles: readonly Tile[]): TileGroup[] {
  return STATUS_ORDER.flatMap(kind => {
    const mine = tiles.filter(tile => tile.status.kind === kind);
    if (mine.length === 0) return [];
    return [{key: kind, title: STATUS_TITLES[kind], tone: STATUS_TONES[kind], tiles: mine}];
  });
}

function keyedGroups(tiles: readonly Tile[], by: Exclude<GroupBy, 'status'>): TileGroup[] {
  const groups = new Map<string, Tile[]>();
  for (const tile of tiles) {
    const key =
      by === 'machine' ? tile.machine : by === 'task' ? tile.task : (tile.agent ?? UNKNOWN_GROUP);
    groups.set(key, [...(groups.get(key) ?? []), tile]);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => (a < b ? -1 : a > b ? 1 : 0))
    .map(([key, mine]) => ({key, title: key, tone: null, tiles: mine}));
}

/**
 * `tiles` in groups: first "Needs you", the runs that need their user; then the rest grouped by
 * `by`: by status, in the order running, paused, failed, finished, stopped, unknown, each under
 * its status word with its dot; otherwise by name, in name order. Every tile is in exactly one
 * group (a run that needs its user is in "Needs you" and not in its status, machine, task or agent
 * group) and each group keeps the order the tiles came in; a group with no tile is left out.
 */
export function groupTiles(tiles: readonly Tile[], by: GroupBy): TileGroup[] {
  const attention = needsYou(tiles);
  const rest = tiles.filter(tile => !tile.attention);
  const groups = by === 'status' ? statusGroups(rest) : keyedGroups(rest, by);
  return attention.length === 0
    ? groups
    : [{key: NEEDS_YOU, title: 'Needs you', tone: 'err', tiles: attention}, ...groups];
}
