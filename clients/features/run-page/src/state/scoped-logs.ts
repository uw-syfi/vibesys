/**
 * The Logs face's streams per scope: the whole run, one idea's spans, or one step's span. A stream
 * belongs to a span when its owner names it (`owner.span_id`); only writers that know their span
 * set it, so a step may have no stream, and the face says so rather than guess.
 */

import type {LogStream} from '@vibesys/contracts/host-helper';
import type {LogView} from '@vibesys/logs';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import {type RunPageState, runHasEnded} from './run-page.js';
import {duration} from './span-reading.js';
import {
  type IdeaOfSpan,
  type LogGroupName,
  type LogLineView,
  logGroups,
  logsNotice,
  NO_IDEAS,
  shownStream,
  streamLines,
} from './views.js';

/** Every span of an idea (its attempts and their steps) to the idea's title, for naming streams. */
export function ideaTitlesBySpan(candidates: readonly Candidate[]): IdeaOfSpan {
  const titles = new Map<string, string>();
  for (const item of candidates) {
    titles.set(item.id, item.title);
    for (const attempt of item.attempts) {
      titles.set(attempt.span, item.title);
      for (const step of attempt.steps) titles.set(step.span, item.title);
    }
  }
  return titles;
}

/** Whose logs: the run's, one idea's, or one step's. */
export type LogScope =
  | {readonly kind: 'run'}
  | {readonly kind: 'idea'; readonly idea: Candidate}
  | {readonly kind: 'step'; readonly step: CandidateStep};

/** The spans a scope covers: null is every stream of the run. */
function scopeSpans(scope: LogScope): ReadonlySet<string> | null {
  switch (scope.kind) {
    case 'run':
      return null;
    case 'step':
      return new Set([scope.step.span]);
    case 'idea':
      return new Set([
        scope.idea.id,
        ...scope.idea.attempts.flatMap(attempt => [
          attempt.span,
          ...attempt.steps.map(step => step.span),
        ]),
      ]);
  }
}

/** One stream of a scope, as a row: its owner, whether it is still written, and its last line. */
interface ScopedStream {
  readonly id: string;
  /** The owner to show beside the label, null when the label already says it (the narration). */
  readonly lead: LogGroupName | null;
  /** What repeats: the startup step a stream ran (a restart or a resume runs it again), else null. */
  readonly repeats: string | null;
  /** How long its startup step took in all, in words, when the run reported it; else null. */
  readonly took: string | null;
  readonly owner: LogGroupName;
  readonly label: string;
  readonly live: boolean;
  /** Null until the stream's lines were fetched (only the shown stream is followed). */
  readonly lastLine: string | null;
}

/**
 * A row of the stream list: one stream, or the repeats of one startup step folded into a row with
 * a count. A fold is open while the shown stream is one of its streams, and then lists them.
 */
type ScopedRow =
  | {readonly kind: 'stream'; readonly stream: ScopedStream; readonly nested: boolean}
  | {
      readonly kind: 'fold';
      readonly id: string;
      readonly label: string;
      readonly count: number;
      /** How long the step took in all, in words, when the run reported it; else null. */
      readonly took: string | null;
      readonly live: boolean;
      readonly lastLine: string | null;
      /** The newest of its streams: what choosing the fold shows. */
      readonly latest: string;
      readonly open: boolean;
    };

export interface ScopedLogs {
  /**
   * The scope's streams grouped by owner, oldest first within a group. The groups with running or
   * the newest work come first, and the startup steps, which the timeline shows, last.
   */
  readonly streams: readonly ScopedStream[];
  /** `streams` as the list shows them, repeated startup steps folded. */
  readonly rows: readonly ScopedRow[];
  /** The stream the pane shows: the selected one if it is in scope, else a default. */
  readonly shown: string | null;
  /** What to say instead of lines, when the scope has no streams. */
  readonly notice: string | null;
}

const lineCache = new WeakMap<LogView, readonly LogLineView[]>();

/** The runtime's boot-stage timing, as the narration shows it (`boot span <name>: <n>ms`). */
const BOOT_TIMING = /^(?:\d\d:\d\d:\d\d \w+ )?boot span \S+: \d+ms/;

/** Whether a line is developer boot timing rather than something the run says to its user. */
function isBootTiming(text: string): boolean {
  return BOOT_TIMING.test(text);
}

function isNarration(state: RunPageState, streamId: string): boolean {
  return listedStreams(state).some(
    stream => stream.id === streamId && stream.owner['kind'] === 'run_narration',
  );
}

/**
 * A stream's lines for `LogPane`, computed once per view: a live log of tens of thousands of lines
 * is re-read on every render of the screen, but only changes when its view does. The run
 * narration leaves out the runtime's boot timing, which is for developers, unless `diagnostics`.
 */
export function paneLines(
  state: RunPageState,
  streamId: string | null,
  diagnostics = false,
): readonly LogLineView[] {
  if (streamId === null) return [];
  const view = state.views[streamId];
  if (view === undefined) return [];
  if (diagnostics) return streamLines(state, streamId);
  const cached = lineCache.get(view);
  if (cached !== undefined) return cached;
  const all = streamLines(state, streamId);
  const lines = isNarration(state, streamId) ? all.filter(line => !isBootTiming(line.text)) : all;
  lineCache.set(view, lines);
  return lines;
}

function ownedStreams(state: RunPageState, spans: ReadonlySet<string> | null): Set<string> | null {
  if (spans === null) return null;
  const streams =
    state.logs.kind === 'listing' || state.logs.kind === 'listed' ? state.logs.streams : [];
  const owner = (value: unknown): boolean => typeof value === 'string' && spans.has(value);
  return new Set(streams.filter(stream => owner(stream.owner['span_id'])).map(stream => stream.id));
}

function listedStreams(state: RunPageState): readonly LogStream[] {
  return state.logs.kind === 'listing' || state.logs.kind === 'listed' ? state.logs.streams : [];
}

/** Startup steps last; the others by whether one is still written, then by the newest stream. */
function orderGroups(
  streams: readonly ScopedStream[],
  listed: readonly LogStream[],
): ScopedStream[] {
  const position = new Map(listed.map((stream, index) => [stream.id, index]));
  const rank = (owner: LogGroupName): readonly [number, number] => {
    const own = streams.filter(stream => stream.owner === owner);
    const newest = Math.max(...own.map(stream => position.get(stream.id) ?? -1));
    return [own.some(stream => stream.live) ? 0 : 1, -newest];
  };
  const owners = [...new Set(streams.map(stream => stream.owner))];
  const ranked = owners
    .filter(owner => owner !== 'Startup')
    .map(owner => ({owner, key: rank(owner)}))
    .sort((a, b) => a.key[0] - b.key[0] || a.key[1] - b.key[1])
    .map(item => item.owner);
  const order: readonly LogGroupName[] = owners.includes('Startup')
    ? [...ranked, 'Startup']
    : ranked;
  return order.flatMap(owner => streams.filter(stream => stream.owner === owner));
}

function foldRows(streams: readonly ScopedStream[], shown: string | null): ScopedRow[] {
  const rows: ScopedRow[] = [];
  const done = new Set<string>();
  for (const stream of streams) {
    const repeats = stream.repeats;
    const same = repeats === null ? [] : streams.filter(other => other.repeats === repeats);
    if (repeats === null || same.length < 2) {
      rows.push({kind: 'stream', stream, nested: false});
      continue;
    }
    if (done.has(repeats)) continue;
    done.add(repeats);
    const latest = same.at(-1) ?? stream;
    const open = same.some(other => other.id === shown);
    rows.push({
      kind: 'fold',
      id: `fold ${repeats}`,
      label: stream.label.replace(/ · \d+$/, ''),
      count: same.length,
      took: stream.took,
      live: same.some(other => other.live),
      lastLine: latest.lastLine,
      latest: latest.id,
      open,
    });
    if (open) for (const other of same) rows.push({kind: 'stream', stream: other, nested: true});
  }
  return rows;
}

/** How long startup step `stage` took, from what the run reported (`CoreState.startupTook`). */
function tookWords(took: Readonly<Record<string, number | undefined>>, stage: string | null) {
  const seconds = stage === null ? undefined : took[stage];
  return seconds === undefined ? null : duration(seconds);
}

/** The scope's streams, the one shown, and the notice for an empty scope. */
export function scopedLogs(
  state: RunPageState,
  scope: LogScope,
  ideas: IdeaOfSpan = NO_IDEAS,
): ScopedLogs {
  const owned = ownedStreams(state, scopeSpans(scope));
  const listed = listedStreams(state);
  const stage = new Map(
    listed.map(stream => [
      stream.id,
      stream.owner['kind'] === 'startup_step' ? String(stream.owner['stage']) : null,
    ]),
  );
  const took = state.run.core?.startupTook ?? {};
  const unordered: ScopedStream[] = logGroups(state, ideas).flatMap(group =>
    group.streams
      .filter(stream => owned === null || owned.has(stream.id))
      .map(stream => ({
        id: stream.id,
        repeats: stage.get(stream.id) ?? null,
        took: tookWords(took, stage.get(stream.id) ?? null),
        lead: stream.label.replace(/ · \d+$/, '') === group.name ? null : group.name,
        owner: group.name,
        label: stream.label,
        live: stream.live,
        lastLine: paneLines(state, stream.id).at(-1)?.text ?? null,
      })),
  );
  const streams = orderGroups(unordered, listed);
  const selected = state.selected;
  let shown: string | null = null;
  if (selected !== null && streams.some(stream => stream.id === selected)) shown = selected;
  else if (owned === null) shown = shownStream(state);
  else shown = (streams.find(stream => stream.live) ?? streams.at(-1))?.id ?? null;
  const notice = streams.length > 0 ? null : emptyNotice(state, scope);
  return {streams, rows: foldRows(streams, shown), shown, notice};
}

/** What the Logs face says when its scope has no stream: the listing's own state, else who wrote none. */
function emptyNotice(state: RunPageState, scope: LogScope): string {
  const listing = logsNotice(state);
  if (listing !== null) return listing;
  const over = runHasEnded(state);
  switch (scope.kind) {
    case 'run':
      return over ? 'This run wrote no logs.' : 'The run has not written any logs yet.';
    case 'idea':
      return over ? 'This idea wrote no logs.' : 'This idea has not written any logs yet.';
    case 'step':
      return 'This step wrote no log.';
  }
}

/**
 * The stream to select so the page follows what the scope shows, or null when it already does: the
 * page fetches lines only for its selected stream, on its Logs tab. The shell dispatches
 * `select-stream` with it while the Logs face is on screen.
 */
export function streamToFollow(state: RunPageState, scope: LogScope): string | null {
  const {shown} = scopedLogs(state, scope);
  return shown !== null && (state.selected !== shown || !state.followingLogs) ? shown : null;
}

/** A line of the run's own history, as the Logs face draws it: a record, or a failure to read them. */
export interface HistoryLine {
  readonly text: string;
  readonly failure: boolean;
}

/**
 * The run's history lines up to sequence `at` (null: all): the records the page holds, then the
 * latest failure among them if the window of records no longer reaches it, so a diagnostic is
 * always a row here; and the failure to read the records, last.
 */
export function historyLines(state: RunPageState, at: number | null): readonly HistoryLine[] {
  const upTo = (seq: string): boolean => at === null || BigInt(seq) <= BigInt(at);
  const rows = state.recordLog ?? [];
  const diagnostic = state.recordDiagnostic;
  const kept =
    diagnostic === undefined || rows.some(row => row.seq === diagnostic.seq)
      ? rows
      : [...rows, diagnostic].sort((a, b) => (BigInt(a.seq) < BigInt(b.seq) ? -1 : 1));
  return [
    ...kept.filter(row => upTo(row.seq)).map(row => ({text: row.message, failure: row.failure})),
    ...(state.recordLogError ? [{text: state.recordLogError, failure: true}] : []),
  ];
}

/** Whether the run's history holds something to look at in Logs: a failure, or none readable. */
export function hasDiagnostic(state: RunPageState): boolean {
  return state.recordDiagnostic !== undefined || Boolean(state.recordLogError);
}
