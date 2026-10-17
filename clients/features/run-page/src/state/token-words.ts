/**
 * Token counts as the Run screen writes them: compact in a line (`48k`, `1.2M`), exact in the
 * hover title beside it. The backend sends counts; this is the one place that words them.
 */
import {type TokenTally, totalTokens} from '@vibesys/run-state';

const SCALES = [
  {unit: 'k', size: 1_000},
  {unit: 'M', size: 1_000_000},
  {unit: 'B', size: 1_000_000_000},
] as const;

/** One decimal, with a whole number written without its `.0`. */
function oneDecimal(value: number): string {
  const text = value.toFixed(1);
  return text.endsWith('.0') ? text.slice(0, -2) : text;
}

/** A count in a line: whole below a thousand, else thousands, millions or billions, one decimal. */
export function compactTokens(count: number): string {
  if (count < SCALES[0].size) return String(count);
  // The smallest scale whose rounded value stays under 1000, so 999 949 is `999.9k` and 999 950 `1M`.
  for (const scale of SCALES) {
    const rounded = Math.round((count / scale.size) * 10) / 10;
    if (rounded < 1000 || scale === SCALES[SCALES.length - 1]) {
      return `${oneDecimal(rounded)}${scale.unit}`;
    }
  }
  return String(count);
}

/** A count in full, grouped by thousands. */
function exactTokens(count: number): string {
  return count.toLocaleString('en-US');
}

/** `48k`: the tally's input plus output tokens; null when the provider reported neither. */
export function tokensFigure(tally: TokenTally): string | null {
  const total = totalTokens(tally);
  return total === null ? null : compactTokens(total);
}

/** A tally as a row shows it: its figure, and the counts in full for the pointer. */
export interface TokensReading {
  readonly label: string;
  readonly title: string;
}

/** The reading of `tally`; null when there is no tally or it reports neither count. */
export function tokensReading(tally: TokenTally | null | undefined): TokensReading | null {
  if (tally === null || tally === undefined) return null;
  const label = tokensFigure(tally);
  return label === null ? null : {label, title: tokensTitle(tally)};
}

/** A cell with nothing in it: a row of a tokens column whose turn reported no counts. */
const BLANK: TokensReading = {label: '', title: ''};

/**
 * The cells of a tokens column, one per row: each row's reading, and a blank cell for a row
 * without one when another row has one, so every row's figures line up. No row has a cell when
 * none has a reading: a list of a run that reports no tokens keeps its width.
 */
export function tokensColumn(
  tallies: readonly (TokenTally | null | undefined)[],
): readonly (TokensReading | null)[] {
  const readings = tallies.map(tokensReading);
  return readings.some(reading => reading !== null)
    ? readings.map(reading => reading ?? BLANK)
    : readings.map(() => null);
}

/** `48k tokens`: the figure with its noun; null when the provider reported neither count. */
export function tokensLabel(tally: TokenTally): string | null {
  const figure = tokensFigure(tally);
  return figure === null ? null : `${figure} tokens`;
}

function part(label: string, count: number | null, within: string): string | null {
  return count === null ? null : `${label} ${exactTokens(count)}${within}`;
}

/**
 * The tally in full, one line per count the provider reported: input with its cached share,
 * output with its reasoning share, and the total. A count never reported has no line.
 */
export function tokensTitle(tally: TokenTally): string {
  const cached = [
    tally.cacheRead === null ? null : `${exactTokens(tally.cacheRead)} cached`,
    tally.cacheWrite === null || tally.cacheWrite === 0
      ? null
      : `${exactTokens(tally.cacheWrite)} written to cache`,
  ].filter((text): text is string => text !== null);
  const reasoning = tally.reasoning === null ? [] : [`${exactTokens(tally.reasoning)} reasoning`];
  const total = totalTokens(tally);
  const lines = [
    part('Input', tally.input, cached.length === 0 ? '' : ` (${cached.join(', ')})`),
    part('Output', tally.output, reasoning.length === 0 ? '' : ` (${reasoning.join(', ')})`),
    total === null ? null : `Total ${exactTokens(total)}`,
  ];
  return lines.filter((line): line is string => line !== null).join('\n');
}
