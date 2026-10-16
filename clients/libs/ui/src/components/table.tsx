import type {JSX, KeyboardEvent, ReactNode} from 'react';

/** One column: its header, how a row fills its cell, and how the cell is set. */
export interface TableColumn<Row> {
  readonly key: string;
  readonly header: string;
  readonly cell: (row: Row) => ReactNode;
  /** Mono and muted, for identifiers, numbers and durations. */
  readonly mono?: boolean;
  readonly align?: 'start' | 'end';
}

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
              <th key={column.key} scope="col" data-align={column.align}>
                {column.header}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {props.rows.map(row => {
            const key = rowKey(row);
            return (
              <tr
                key={key}
                data-selectable={select === undefined ? undefined : true}
                aria-current={key === props.selected ? 'true' : undefined}
                tabIndex={select === undefined ? undefined : 0}
                onClick={select === undefined ? undefined : () => select(key)}
                onKeyDown={select === undefined ? undefined : event => keyDown(event, key)}
              >
                {columns.map(column => (
                  <td key={column.key} data-mono={column.mono} data-align={column.align}>
                    {column.cell(row)}
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
