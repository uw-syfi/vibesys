import type {JSX, KeyboardEvent, ReactNode} from 'react';
import {Icon} from './icon.js';

/** One column: its header, how a row fills its cell, and how the cell is set. */
export interface TableColumn<Row> {
  readonly key: string;
  readonly header: string;
  readonly cell: (row: Row) => ReactNode;
  /** Mono and muted, for identifiers, numbers and durations. */
  readonly mono?: boolean;
  readonly align?: 'start' | 'end';
  /** Shown only when the table has room: it hides below `NARROW` of its own width. */
  readonly wide?: boolean;
  /** The column that names the row: it keeps a readable width, and the table scrolls instead. */
  readonly lead?: boolean;
  /**
   * A column of buttons: its header is not drawn (it stays the column's accessible name) and each
   * cell is centred on the text of its row.
   */
  readonly control?: boolean;
}

/** What a row's first cell offers to fold or unfold what the row stands for. */
export interface TableFold {
  readonly open: boolean;
  readonly toggle: () => void;
  /** Names the control (`Expand Planner`). */
  readonly label: string;
}

/** A row beneath another (`sub`), or the table's total (`total`): neither can be selected. */
export type TableRowKind = 'sub' | 'total';

/**
 * A table of `rows` in `columns`, with a sticky header, scrolling sideways in its own box when it is
 * wider than its container. With `select`, a press or Enter on a row selects it, and the row whose
 * key is `selected` is marked current.
 */
export function Table<Row>(props: {
  /** The table's accessible name, also naming its scrolling box. */
  readonly label: string;
  readonly columns: readonly TableColumn<Row>[];
  readonly rows: readonly Row[];
  readonly rowKey: (row: Row) => string;
  readonly selected?: string;
  readonly select?: (key: string) => void;
  readonly rowKind?: (row: Row) => TableRowKind | undefined;
  /** A row that folds gets a chevron before its first cell; `'none'` keeps the slot empty. */
  readonly fold?: (row: Row) => TableFold | 'none';
}): JSX.Element {
  const {columns, rowKey, select} = props;
  const keyDown = (event: KeyboardEvent<HTMLTableRowElement>, key: string): void => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    select?.(key);
  };
  return (
    <section
      className="vs-table-scroll"
      aria-label={props.label}
      // biome-ignore lint/a11y/noNoninteractiveTabindex: a box that scrolls sideways must take the focus so a keyboard can scroll it (WCAG 2.1.1, axe scrollable-region-focusable); no interactive element scrolls a table.
      tabIndex={0}
    >
      <table className="vs-table" aria-label={props.label}>
        <thead>
          <tr>
            {columns.map(column => (
              <th
                key={column.key}
                scope="col"
                data-align={column.align}
                data-wide={column.wide === true || undefined}
              >
                {column.control === true ? (
                  <span className="vs-table-hidden-header">{column.header}</span>
                ) : (
                  column.header
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map(row => {
            const key = rowKey(row);
            const kind = props.rowKind?.(row);
            const pickable = select !== undefined && kind === undefined;
            return (
              <tr
                key={key}
                data-kind={kind}
                data-selectable={pickable ? true : undefined}
                aria-current={key === props.selected ? 'true' : undefined}
                tabIndex={pickable ? 0 : undefined}
                onClick={pickable ? () => select(key) : undefined}
                onKeyDown={pickable ? event => keyDown(event, key) : undefined}
              >
                {columns.map((column, at) => (
                  <td
                    key={column.key}
                    data-mono={column.mono}
                    data-mono-id={column.mono}
                    data-align={column.align}
                    data-wide={column.wide === true || undefined}
                    data-lead={column.lead === true || undefined}
                    data-control={column.control === true || undefined}
                  >
                    {at === 0 && props.fold !== undefined ? (
                      <span className="vs-table-lead">
                        <FoldCell fold={props.fold(row)} />
                        {column.cell(row)}
                      </span>
                    ) : (
                      column.cell(row)
                    )}
                  </td>
                ))}
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

function FoldCell(props: {readonly fold: TableFold | 'none'}): JSX.Element {
  const {fold} = props;
  if (fold === 'none') return <span className="vs-table-fold" />;
  return (
    <button
      type="button"
      className="vs-table-fold"
      aria-expanded={fold.open}
      aria-label={fold.label}
      onClick={event => {
        event.stopPropagation();
        fold.toggle();
      }}
    >
      <Icon name={fold.open ? 'chevron-down' : 'chevron-right'} />
    </button>
  );
}
