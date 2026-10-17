/**
 * The Logs face's streams per scope: the whole run, one idea's spans, or one step's span. A stream
 * belongs to a span when its owner names it (`owner.span_id`); only writers that know their span
 * set it, so a step may have no stream, and the face says so rather than guess.
 */

import type {LogView} from '@vibesys/logs';
import type {Candidate, CandidateStep} from '@vibesys/run-state/candidates';
import type {RunPageState} from './run-page.js';
import {
  type LogGroupName,
  type LogLineView,
  logGroups,
  logsNotice,
  shownStream,
  streamLines,
} from './views.js';

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
  readonly owner: LogGroupName;
  readonly label: string;
  readonly live: boolean;
  /** Null until the stream's lines were fetched (only the shown stream is followed). */
  readonly lastLine: string | null;
}

export interface ScopedLogs {
  /** The scope's streams grouped by owner, oldest first within a group. */
  readonly streams: readonly ScopedStream[];
  /** The stream the pane shows: the selected one if it is in scope, else a default. */
  readonly shown: string | null;
  /** What to say instead of lines, when the scope has no streams. */
  readonly notice: string | null;
}

const lineCache = new WeakMap<LogView, readonly LogLineView[]>();

/**
 * A stream's lines for `LogPane`, computed once per view: a live log of tens of thousands of lines
 * is re-read on every render of the screen, but only changes when its view does.
 */
export function paneLines(state: RunPageState, streamId: string | null): readonly LogLineView[] {
  if (streamId === null) return [];
  const view = state.views[streamId];
  if (view === undefined) return [];
  const cached = lineCache.get(view);
  if (cached !== undefined) return cached;
  const lines = streamLines(state, streamId);
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

/** The scope's streams, the one shown, and the notice for an empty scope. */
export function scopedLogs(state: RunPageState, scope: LogScope): ScopedLogs {
  const owned = ownedStreams(state, scopeSpans(scope));
  const streams: ScopedStream[] = logGroups(state).flatMap(group =>
    group.streams
      .filter(stream => owned === null || owned.has(stream.id))
      .map(stream => ({
        id: stream.id,
        owner: group.name,
        label: stream.label,
        live: stream.live,
        lastLine: paneLines(state, stream.id).at(-1)?.text ?? null,
      })),
  );
  const selected = state.selected;
  let shown: string | null = null;
  if (selected !== null && streams.some(stream => stream.id === selected)) shown = selected;
  else if (owned === null) shown = shownStream(state);
  else shown = (streams.find(stream => stream.live) ?? streams.at(-1))?.id ?? null;
  const notice = streams.length > 0 ? null : emptyNotice(state, scope);
  return {streams, shown, notice};
}

function emptyNotice(state: RunPageState, scope: LogScope): string | null {
  const run = logsNotice(state);
  if (scope.kind === 'run' || run !== null) return run ?? 'The run has not written any logs yet.';
  return scope.kind === 'idea'
    ? 'None of this idea’s steps wrote a log.'
    : 'This step wrote no log.';
}

/**
 * The stream to select so the page follows what the scope shows, or null when it already does: the
 * page fetches lines only for its selected stream, on its Logs tab. The shell dispatches
 * `select-stream` with it while the Logs face is on screen.
 */
export function streamToFollow(state: RunPageState, scope: LogScope): string | null {
  const {shown} = scopedLogs(state, scope);
  return shown !== null && (state.selected !== shown || state.tab !== 'logs') ? shown : null;
}
