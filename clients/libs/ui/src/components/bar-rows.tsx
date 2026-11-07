import type {JSX} from 'react';
import {MONO_ADVANCE, SPACE, TYPE} from '../tokens/index.js';
import type {StyleWithVars} from './css-vars.js';
import {useInlineSize} from './label-fit.js';
import {Swatch} from './swatch-legend.js';
import {Text} from './text.js';

/** One figure column of `BarRows`: its header, and whether it is the row's own measure. */
export interface BarColumn {
  readonly header: string;
  /** The column the bars stand for: drawn in the text color, the others muted. */
  readonly strong?: boolean;
}

/** A tuple of `N` of `T`; the open list when `N` is not a literal. */
type TupleOf<T, N extends number, Built extends readonly T[] = readonly []> = number extends N
  ? readonly T[]
  : Built['length'] extends N
    ? Built
    : TupleOf<T, N, readonly [T, ...Built]>;

/**
 * The figures of one row of a table with `N` columns: exactly one for each, as it is written; null
 * is a figure that was not reported. A row built for another number of columns does not type-check
 * against the table it is given to, so a header can never stand over another column's figure.
 */
export type BarFigures<N extends number = number> = TupleOf<string | null, N>;

/** One row of a table with `N` figure columns: a name, a bar and a figure for each column. */
export interface BarRow<N extends number = number> {
  readonly key: string;
  /** What the row is, cut with an ellipsis when the column is narrow; its whole text is the hover. */
  readonly name: string;
  /** The name is an identifier a person may copy (a model id), set in the code face. */
  readonly mono?: boolean;
  /** The row's swatch, and the color of its bar, is palette color number `series`. */
  readonly series?: number;
  /** A short word after the name that says what is missing from the row ("No price"). */
  readonly note?: string;
  /** What the bar measures, drawn as wide as its share of the largest value of the rows; null draws no bar. */
  readonly value: number | null;
  /** One figure for each column. */
  readonly figures: BarFigures<N>;
}

/** The room of a glyph of the figure face and of the header face, in pixels. */
const FIGURE_GLYPH = TYPE.code.size * MONO_ADVANCE;
const HEADER_GLYPH = TYPE.caption.size * MONO_ADVANCE;
/** The air before every column but the first. */
const GAP = SPACE['space-4'];
/** What the name and the bar need so that neither collapses, in pixels. */
const NAME_FLOOR = SPACE['space-8'] * 3;
const BAR_FLOOR = SPACE['space-8'] * 2;
/** What a figure that was not reported is written as. */
const NOT_REPORTED = '—';

function widthOf(column: BarColumn, at: number, rows: readonly BarRow[]): number {
  const figures = rows.map(row => (row.figures[at] ?? NOT_REPORTED).length);
  return Math.ceil(
    Math.max(column.header.length * HEADER_GLYPH, Math.max(0, ...figures) * FIGURE_GLYPH) + GAP,
  );
}

interface BarRowsProps<Columns extends readonly BarColumn[]> {
  readonly label: string;
  readonly nameHeader: string;
  readonly barLabel: string;
  readonly columns: Columns;
  /** One figure per column in every row: a row of another width is a type error. */
  readonly rows: readonly BarRow<Columns['length']>[];
}

/**
 * Rows of a name, a bar and figures, every row on one grid: the bars start and end at the same
 * place, and each figure column is as wide as its widest figure and header and right-aligned in
 * tabular figures, so a digit gained moves nothing. A bar is as wide as its row's `value` is of
 * the largest, a layout scale only; the figures are the caller's words. A row with no `value`
 * draws no bar. A row's `note` follows its name, and drops under it when the name leaves no room. When the width cannot hold a readable name, bar and figures, the bars give way
 * first. `label` names the table; `barLabel` names the bar column for assistive technology.
 */
export function BarRows<const Columns extends readonly BarColumn[]>(
  props: BarRowsProps<Columns>,
): JSX.Element;
export function BarRows(props: BarRowsProps<readonly BarColumn[]>): JSX.Element {
  const [measure, width] = useInlineSize();
  const widths = props.columns.map((column, at) => widthOf(column, at, props.rows));
  const figures = widths.reduce((sum, each) => sum + each, 0);
  const barFits = width === null || width >= NAME_FLOOR + BAR_FLOOR + GAP + figures;
  const largest = Math.max(0, ...props.rows.map(row => row.value ?? 0));
  return (
    <div ref={measure} className="vs-bar-rows-box">
      <table className="vs-bar-rows" aria-label={props.label} data-bar={barFits ? '' : 'off'}>
        <colgroup>
          <col className="vs-bar-rows-name-col" />
          <col className="vs-bar-rows-bar-col" />
          {widths.map((each, at) => {
            const style: StyleWithVars = {'--col-width': `${each}px`};
            return <col key={props.columns[at]?.header ?? at} style={style} />;
          })}
        </colgroup>
        <thead>
          <tr>
            <th scope="col">{props.nameHeader}</th>
            <th scope="col" data-role="bar">
              <span className="vs-table-hidden-header">{props.barLabel}</span>
            </th>
            {props.columns.map(column => (
              <th key={column.header} scope="col" data-align="end">
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map(row => {
            const share = row.value === null || largest <= 0 ? 0 : Math.max(0, row.value) / largest;
            const fill: StyleWithVars = {
              '--bar-share': `${(share * 100).toFixed(1)}%`,
              ...(row.series === undefined ? {} : {'--seg-color': `var(--series-${row.series})`}),
            };
            return (
              <tr key={row.key}>
                <th scope="row" className="vs-bar-rows-name">
                  <span className="vs-bar-rows-lead">
                    {row.series === undefined ? null : <Swatch series={row.series} />}
                    <span className="vs-bar-rows-text">
                      <span
                        className="vs-bar-rows-label"
                        data-mono={row.mono === true || undefined}
                        title={row.name}
                      >
                        {row.name}
                      </span>
                      {row.note === undefined ? null : (
                        <Text variant="small" tone="muted">
                          {row.note}
                        </Text>
                      )}
                    </span>
                  </span>
                </th>
                <td data-role="bar">
                  <span className="vs-bar-rows-track" aria-hidden="true" style={fill}>
                    <span className="vs-bar-rows-fill" />
                  </span>
                </td>
                {props.columns.map((column, at) => {
                  const figure = row.figures[at] ?? null;
                  return (
                    <td
                      key={column.header}
                      data-align="end"
                      data-strong={column.strong === true || undefined}
                      data-missing={figure === null || undefined}
                    >
                      {figure ?? NOT_REPORTED}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
