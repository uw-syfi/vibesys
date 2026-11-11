import type {JSX, KeyboardEvent, ReactNode, SyntheticEvent} from 'react';
import {Icon} from './icon.js';
import {ScrollArea} from './scroll-area.js';

/** One column: its header, how a row fills its cell, and how the cell is set. */
export interface TableColumn<Row> {
  readonly key: string;
  readonly header: string;
  /** What the header means, on the pointer and as its description (a unit, a basis): for a short header. */
  readonly hint?: string;
  readonly cell: (row: Row) => ReactNode;
  /** What a column's values are counted in, written small under its header. */
  readonly unit?: string;
  /** The header is a control that sorts the table by this column (see `sort`). */
  readonly sortable?: boolean;
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
  /**
   * A cell of text a person chose (a name), cut short with an ellipsis where the column is at its
   * widest instead of widening the table. `clip` returns the whole text, which the pointer shows.
   */
  readonly clip?: (row: Row) => string;
}

/** Whether `event` came from a control inside its row, which acts alone and does not pick the row. */
function fromControl(event: SyntheticEvent<HTMLTableRowElement>): boolean {
  const control =
    event.target instanceof Element
      ? event.target.closest('button, a, input, select, textarea, label')
      : null;
  return (
    control !== null && control !== event.currentTarget && event.currentTarget.contains(control)
  );
}

/** What a row's first cell offers to fold or unfold what the row stands for. */
export interface TableFold {
  readonly open: boolean;
  readonly toggle: () => void;
  /** Names the control (`Expand Planner`). */
  readonly label: string;
}

/** How a table is ordered: by which column, which way, and what pressing a sortable header asks for. */
export interface TableSort {
  readonly key: string;
  readonly direction: 'ascending' | 'descending';
  /** A press of a sortable header: the same column turns the order over, another column starts over. */
  readonly change: (key: string) => void;
}

/** A row beneath another (`sub`), or the table's total (`total`): neither can be selected. */
export type TableRowKind = 'sub' | 'total';

/**
 * A table of `rows` in `columns`, with a sticky header, scrolling sideways in its own box when it is
 * wider than its container. With `select`, a press or Enter on a row selects it, and the row whose
 * key is `selected` is marked current. With `middle`, a row's cells are centered on each other, for
 * rows that hold a control beside text.
 */
export function Table<Row>(props: {
  /** The table's accessible name, also naming its scrolling box. */
  readonly label: string;
  readonly columns: readonly TableColumn<Row>[];
  readonly rows: readonly Row[];
  readonly rowKey: (row: Row) => string;
  readonly selected?: string;
  readonly middle?: boolean;
  /** Rows set close, for a table that holds many: the least padding the scale has. */
  readonly dense?: boolean;
  /**
   * Where a table that outgrows its box scrolls: `across` (the default) when a column cannot be
   * narrower, or `down`, in a box as tall as `table-rows` with the header kept in view, for a
   * table with many rows whose cells cut their text instead of widening.
   */
  readonly scroll?: 'across' | 'down';
  /** The order the rows are in, and the control that asks for another. */
  readonly sort?: TableSort;
  /**
   * What the table says when it has no rows: one row under its headers, in a quiet voice, so the
   * table holds its columns and its place until the first row comes.
   */
  readonly empty?: string;
  /** The rows are being read again: they stay, quieter, until the answer replaces them. */
  readonly dim?: boolean;
  readonly select?: (key: string) => void;
  readonly rowKind?: (row: Row) => TableRowKind | undefined;
  /** A row that folds gets a chevron before its first cell; `'none'` keeps the slot empty. */
  readonly fold?: (row: Row) => TableFold | 'none';
}): JSX.Element {
  const {columns, rowKey, select} = props;
  const keyDown = (event: KeyboardEvent<HTMLTableRowElement>, key: string): void => {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    if (fromControl(event)) return;
    event.preventDefault();
    select?.(key);
  };
  return (
    <ScrollArea
      as="section"
      kind={props.scroll === 'down' ? 'feed' : 'table'}
      className="vs-table-scroll"
      data-down={props.scroll === 'down' || undefined}
      aria-label={props.label}
      tabIndex={0}
    >
      <table
        className="vs-table"
        aria-label={props.label}
        data-middle={props.middle || undefined}
        data-dense={props.dense || undefined}
        data-dim={props.dim || undefined}
      >
        <thead>
          <tr>
            {columns.map(column => (
              <th
                key={column.key}
                scope="col"
                data-align={column.align}
                data-wide={column.wide === true || undefined}
                data-lead={column.lead === true || undefined}
                data-clip={column.clip !== undefined || undefined}
                aria-sort={sortOf(props.sort, column)}
                title={column.hint}
              >
                {column.control === true ? (
                  <span className="vs-table-hidden-header">{column.header}</span>
                ) : column.sortable === true && props.sort !== undefined ? (
                  <SortHeader column={column} sort={props.sort} />
                ) : (
                  <>
                    {column.header}
                    {column.unit === undefined ? null : (
                      <span className="vs-table-unit">{column.unit}</span>
                    )}
                  </>
                )}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.length === 0 && props.empty !== undefined ? (
            <tr data-kind="empty">
              <td colSpan={columns.length}>{props.empty}</td>
            </tr>
          ) : null}
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
                onClick={
                  pickable ? event => (fromControl(event) ? undefined : select(key)) : undefined
                }
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
                    data-clip={column.clip !== undefined || undefined}
                    title={column.clip?.(row)}
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
    </ScrollArea>
  );
}

/** What a header says of the order: `aria-sort` on the column the rows are sorted by. */
function sortOf<Row>(
  sort: TableSort | undefined,
  column: TableColumn<Row>,
): 'ascending' | 'descending' | undefined {
  return column.sortable === true && sort?.key === column.key ? sort.direction : undefined;
}

/** A header that sorts: its words as a button, with the arrow of the order on the column in force. */
function SortHeader<Row>(props: {
  readonly column: TableColumn<Row>;
  readonly sort: TableSort;
}): JSX.Element {
  const {column, sort} = props;
  const active = sort.key === column.key;
  return (
    <button
      type="button"
      className="vs-table-sort"
      data-active={active || undefined}
      data-direction={active ? sort.direction : undefined}
      onClick={() => sort.change(column.key)}
    >
      {column.header}
      <span className="vs-table-sort-arrow" aria-hidden="true">
        <Icon name="arrow-up" />
      </span>
    </button>
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
