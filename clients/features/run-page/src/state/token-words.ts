/**
 * Token counts as the Run screen writes them: compact in a line (`48k`, `1.2M`), exact in the
 * hover title beside it. The backend sends counts; this is the one place that words them.
 *
 * Input and output are shown apart, and the input's cached share beside it: `in 7.1M (6.2M
 * cached) · out 0.5M`. The input includes the cached part, as the provider reports it, so the
 * uncached part is the input less the cached one. A count the provider did not report is written
 * `unknown`, never `0`; a real zero reads as `0`.
 */
import type {TokenTally} from '@vibesys/run-state';
import {compactTokens} from '@vibesys/run-state/usage';

/** What a count nobody reported is written as. */
const UNKNOWN = 'unknown';

/** A count in full, grouped by thousands. */
function exactTokens(count: number): string {
  return count.toLocaleString('en-US');
}

/** A count in a line, or `unknown` when it was not reported. */
function compactOrUnknown(count: number | null): string {
  return count === null ? UNKNOWN : compactTokens(count);
}

/** Whether the provider reported any count for the tally. */
function reported(tally: TokenTally): boolean {
  return tally.input !== null || tally.output !== null;
}

/** The cached share of the input: `6.2M cached`, `cached unknown` when only that is missing. */
function cachedWords(tally: TokenTally): string | null {
  if (tally.input === null)
    return tally.cacheRead === null ? null : `${compactTokens(tally.cacheRead)} cached`;
  return tally.cacheRead === null
    ? `cached ${UNKNOWN}`
    : `${compactTokens(tally.cacheRead)} cached`;
}

/**
 * `in 7.1M (6.2M cached) · out 0.5M`: input with its cached share, then output; null when the
 * provider reported neither count. A part it did not report reads `unknown`.
 */
export function tokensLine(tally: TokenTally): string | null {
  if (!reported(tally)) return null;
  const cached = cachedWords(tally);
  const input = `in ${compactOrUnknown(tally.input)}${cached === null ? '' : ` (${cached})`}`;
  return `${input} · out ${compactOrUnknown(tally.output)}`;
}

function part(label: string, count: number | null, within: readonly string[]): string {
  const detail = within.length === 0 ? '' : ` (${within.join(', ')})`;
  return `${label} ${count === null ? UNKNOWN : exactTokens(count)}${detail}`;
}

/** The input's own parts in full: cached, uncached, and what was written to the cache when any. */
function inputParts(tally: TokenTally): string[] {
  const {input, cacheRead, cacheWrite} = tally;
  const parts: string[] = [];
  if (input !== null) {
    if (cacheRead === null) parts.push(`cached ${UNKNOWN}`);
    else
      parts.push(
        `${exactTokens(cacheRead)} cached`,
        `${exactTokens(Math.max(0, input - cacheRead))} uncached`,
      );
  } else if (cacheRead !== null) {
    parts.push(`${exactTokens(cacheRead)} cached`);
  }
  if (cacheWrite !== null && cacheWrite > 0)
    parts.push(`${exactTokens(cacheWrite)} written to cache`);
  return parts;
}

/**
 * The tally in full, one line per part: input with its cached and uncached counts (and the cache
 * writes when there were any), output with its reasoning share when there was one, and the total
 * once both are known. A part nobody reported reads `unknown`.
 */
export function tokensTitle(tally: TokenTally): string {
  const reasoning =
    tally.reasoning !== null && tally.reasoning > 0
      ? [`${exactTokens(tally.reasoning)} reasoning`]
      : [];
  const total =
    tally.input !== null && tally.output !== null
      ? [`Total ${exactTokens(tally.input + tally.output)}`]
      : [];
  return [
    part('Input', tally.input, inputParts(tally)),
    part('Output', tally.output, reasoning),
    ...total,
  ].join('\n');
}

/** A tally as a line shows it: the `in … · out …` words, and the counts in full for the pointer. */
export interface TokensReading {
  readonly label: string;
  readonly title: string;
}

/** The reading of `tally`; null when there is no tally or it reports neither count. */
export function tokensReading(tally: TokenTally | null | undefined): TokensReading | null {
  if (tally === null || tally === undefined) return null;
  const label = tokensLine(tally);
  return label === null ? null : {label, title: tokensTitle(tally)};
}

/**
 * What one row of a tokens column knows: a tally the turn reported, `running` while the turn has
 * not ended (its counts come when it does), `null` for a turn that reported nothing, and
 * `undefined` for a row that is no turn (a gate), which has no tokens at all.
 */
export type TurnSpend = TokenTally | 'running' | null | undefined;

const RUNNING_TITLE = 'Still running: its tokens are counted when the turn ends';
const NOT_REPORTED_TITLE = 'This turn reported no token counts';

/** A cell with nothing in it: a row that is no turn, in a column the other rows fill. */
const BLANK: TokensReading = {label: '', title: ''};

function readingOf(spend: TurnSpend): TokensReading | null {
  if (spend === undefined) return null;
  if (spend === 'running') return {label: 'running', title: RUNNING_TITLE};
  if (spend === null) return {label: UNKNOWN, title: NOT_REPORTED_TITLE};
  return tokensReading(spend) ?? {label: UNKNOWN, title: NOT_REPORTED_TITLE};
}

/**
 * Whether any row holds a reported figure, which is what a column is for: a run whose provider
 * reports no tokens has no column, however many turns are running.
 */
function anyFigure(rows: readonly TurnSpend[]): boolean {
  return rows.some(
    spend => spend !== 'running' && spend !== null && spend !== undefined && reported(spend),
  );
}

/**
 * The cells of a tokens column, one per row: each row's reading, `running` for a running turn,
 * `unknown` for a turn that reported nothing, and a blank cell for a row that is no turn, so
 * every row's figures line up. No row has a cell when none has a reported figure (running turns
 * do not make one): a list of a run that reports no tokens keeps its width.
 */
export function tokensColumn(rows: readonly TurnSpend[]): readonly (TokensReading | null)[] {
  if (!anyFigure(rows)) return rows.map(() => null);
  return rows.map(spend => (spend === undefined ? BLANK : readingOf(spend)));
}

/** One cell of the input or output column: a figure, a quiet second value, and the full counts. */
export interface TokensCell {
  readonly label: string;
  /** A quiet value beside the label (the input's cached share); empty when there is none. */
  readonly note: string;
  readonly title: string;
}

/** The two columns of a row: what it took in (and how much of that was cached) and what it wrote. */
export interface TokensCells {
  readonly input: TokensCell;
  readonly output: TokensCell;
}

const BLANK_CELL: TokensCell = {label: '', note: '', title: ''};
const BLANK_CELLS: TokensCells = {input: BLANK_CELL, output: BLANK_CELL};

function cellsOf(spend: TurnSpend): TokensCells {
  if (spend === undefined) return BLANK_CELLS;
  if (spend === 'running') {
    return {input: {label: 'running', note: '', title: RUNNING_TITLE}, output: BLANK_CELL};
  }
  if (spend === null || !reported(spend)) {
    const cell = {label: UNKNOWN, note: '', title: NOT_REPORTED_TITLE};
    return {input: cell, output: cell};
  }
  const title = tokensTitle(spend);
  return {
    input: {label: compactOrUnknown(spend.input), note: cachedWords(spend) ?? '', title},
    output: {label: compactOrUnknown(spend.output), note: '', title},
  };
}

/** The input and output cells of each row, or no cells when no row has a figure (see `tokensColumn`). */
export function tokensCells(rows: readonly TurnSpend[]): readonly (TokensCells | null)[] {
  if (!anyFigure(rows)) return rows.map(() => null);
  return rows.map(cellsOf);
}
