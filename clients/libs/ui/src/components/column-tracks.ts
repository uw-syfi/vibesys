/**
 * The grid of a `ColumnList`: each column's track, and the tier a list of a given width can show.
 * A list shows every column while each one's least width fits, and gives up its optional columns
 * only when they do not; so the width at which a column goes is the columns' own, not a constant.
 */
import {SIZE, SPACE} from '../tokens/index.js';

/**
 * How wide a column is: the rest of the row (`fill`), what a name or a figure takes (`wide`,
 * `medium`, `short`, `compact`), a mark and a few digits (`dot`), or a column that gives up its room
 * to `fill` before any other does and is cut with an ellipsis (`shrink`).
 */
export type ColumnSize = 'fill' | 'shrink' | 'wide' | 'medium' | 'short' | 'compact' | 'dot';

/** What a list shows: every column, the columns not marked optional, or those at their tight size. */
export type ColumnTier = 'wide' | 'narrow' | 'tight';

/** The columns' sizes a tier reads. */
export interface TrackedColumn {
  readonly size: ColumnSize;
  readonly optional?: boolean | undefined;
  readonly narrow?: ColumnSize | undefined;
  readonly tight?: ColumnSize | undefined;
}

interface Track {
  /** The column is never narrower than this. */
  readonly least: number;
  /** A share of the row's slack the column takes beyond its least; null: it takes none. */
  readonly share: number | null;
}

const FIGURE = SIZE['gantt-figure'];

const TRACKS: Readonly<Record<ColumnSize, Track>> = {
  fill: {least: SIZE['column-tile'], share: 1},
  shrink: {least: Math.round(FIGURE * 0.75), share: 0.35},
  wide: {least: FIGURE + SPACE['space-8'], share: null},
  medium: {least: FIGURE + SPACE['space-3'], share: null},
  short: {least: FIGURE, share: null},
  compact: {least: Math.round(FIGURE * 0.7), share: null},
  dot: {least: SIZE.dot + SPACE['space-6'], share: null},
};

/** A list narrower than this, crowded with columns, takes its tight sizes. */
const TIGHT_WIDTH = 560;
/** The gap between columns while every column shows. */
const WIDE_GAP = SPACE['space-3'];
/** What a row keeps to its sides: its padding, its marker bar and its border. */
const ROW_INSET = 2 * SPACE['space-3'] + SIZE['focus-ring'] + SIZE.hairline;
/** The room a scrollbar may take from the rows. */
const SCROLLBAR = SPACE['space-5'];

function sizeIn(column: TrackedColumn, tier: ColumnTier): ColumnSize {
  if (tier === 'wide') return column.size;
  return (tier === 'tight' ? column.tight : undefined) ?? column.narrow ?? column.size;
}

/** The grid template of `columns` at `tier`; the optional columns are not in a narrower tier's. */
export function tracksOf(columns: readonly TrackedColumn[], tier: ColumnTier): string {
  return columns
    .filter(column => tier === 'wide' || column.optional !== true)
    .map(column => {
      const size = sizeIn(column, tier);
      if (tier !== 'wide' && column.size === 'fill') return 'minmax(0, 1fr)';
      const {least, share} = TRACKS[size];
      return share === null ? `${least}px` : `minmax(${least}px, ${share}fr)`;
    })
    .join(' ');
}

/** The width every column needs to show at its least. */
export function widthToShowAll(columns: readonly TrackedColumn[]): number {
  const least = columns.reduce((sum, column) => sum + TRACKS[column.size].least, 0);
  return least + WIDE_GAP * Math.max(0, columns.length - 1) + ROW_INSET + SCROLLBAR;
}

/**
 * The tier for a list `width` pixels wide (null: not measured, so it shows everything). A crowded
 * list (a column for each of several objectives) takes its tight sizes when it is also narrow.
 */
export function columnTier(
  width: number | null,
  columns: readonly TrackedColumn[],
  crowded: boolean,
): ColumnTier {
  if (width === null || width >= widthToShowAll(columns)) return 'wide';
  return crowded && width <= TIGHT_WIDTH ? 'tight' : 'narrow';
}
