import {type CSSProperties, type FocusEvent, type JSX, type ReactNode, useId, useRef} from 'react';
import {SIZE} from '../tokens/index.js';
import {navigationKeys, pickKeys} from './listbox.js';
import {Text} from './text.js';
import {InSlot, useSlot, useWindowed} from './windowing.js';

/** How wide a column is: the rest of the row, a name's width, or a figure's. */
export type ColumnSize = 'fill' | 'wide' | 'short';

const TRACKS: Readonly<Record<ColumnSize, string>> = {
  fill: 'minmax(0, 1fr)',
  wide: 'var(--column-swatch)',
  short: 'var(--gantt-figure)',
};

/** One column of a `ColumnList`: its header, how a row fills its cell, and how the header sorts. */
export interface ListColumn<Row> {
  readonly key: string;
  readonly header: string;
  readonly size: ColumnSize;
  readonly align?: 'start' | 'end';
  /** Drawn only while the list has room: it gives way when the list is narrow. */
  readonly optional?: boolean;
  /** The column's size when the list is narrow; the same by default. */
  readonly narrow?: ColumnSize;
  /** What the pointer shows over a cell whole, for a cell that may be cut short. */
  readonly hint?: (row: Row) => string | undefined;
  readonly cell: (row: Row) => ReactNode;
  /** Pressing the header sorts by the column; `marked` draws the arrow on the one it sorts by. */
  readonly sort?: {readonly marked: boolean; readonly press: () => void};
}

/** What a row of a `ColumnList` is marked as, beside whether it is the selected one. */
export interface RowMarks {
  /** On the path that leads to the selected row: a bar in the accent. */
  readonly lineage?: boolean;
  /** Changed while on screen: the row flashes once. */
  readonly fresh?: boolean;
}

function Header<Row>(props: {
  readonly columns: readonly ListColumn<Row>[];
  readonly id: string;
}): JSX.Element {
  return (
    <div className="vs-columnlist-head" id={props.id}>
      {props.columns.map(column => (
        <span
          key={column.key}
          className="vs-columnlist-headcell"
          data-align={column.align}
          data-optional={column.optional || undefined}
          data-marked={column.sort?.marked || undefined}
        >
          {column.sort === undefined ? (
            column.header
          ) : (
            <button type="button" onClick={column.sort.press} title={`Sort by ${column.header}`}>
              {column.header}
              {column.sort.marked ? <span aria-hidden="true">{' ▾'}</span> : null}
            </button>
          )}
        </span>
      ))}
    </div>
  );
}

function Row<Row>(props: {
  readonly id: string;
  readonly row: Row;
  readonly columns: readonly ListColumn<Row>[];
  readonly selected: boolean;
  readonly marks: RowMarks;
  readonly select: () => void;
  readonly hover: ((on: boolean) => void) | undefined;
}): JSX.Element {
  const slot = useSlot();
  const {hover} = props;
  return (
    <div
      className="vs-columnlist-row"
      ref={slot?.measure}
      role="option"
      tabIndex={props.selected ? 0 : -1}
      aria-selected={props.selected}
      aria-posinset={slot === null ? undefined : slot.index + 1}
      aria-setsize={slot === null ? undefined : slot.size}
      data-lineage={props.marks.lineage || undefined}
      data-fresh={props.marks.fresh || undefined}
      onClick={event => {
        // A link inside a cell acts on its own.
        if (event.target instanceof Element && event.target.closest('button, a') !== null) return;
        props.select();
      }}
      onMouseEnter={hover === undefined ? undefined : () => hover(true)}
      onMouseLeave={hover === undefined ? undefined : () => hover(false)}
      onFocus={hover === undefined ? undefined : () => hover(true)}
      onBlur={hover === undefined ? undefined : () => hover(false)}
      onKeyDown={event => {
        if (event.target === event.currentTarget) pickKeys(event, props.select);
      }}
    >
      {props.columns.map(column => (
        <span
          key={column.key}
          className="vs-columnlist-cell"
          data-align={column.align}
          data-optional={column.optional || undefined}
          title={column.hint?.(props.row)}
        >
          {column.cell(props.row)}
        </span>
      ))}
    </div>
  );
}

/**
 * A list of rows in columns to choose from: a listbox with one tab stop, whose header names the
 * columns and sorts by the ones that offer it. It scrolls inside itself and draws only the rows near
 * the visible part, so any number of rows costs a screenful (the same windowing as `PickList`). The
 * keys reach every row: Up and Down step, Page Up and Down move a screenful, Home and End jump,
 * Enter and Space pick. `selected` is the picked row's key; `marks` adds a bar to rows on its path.
 */
export function ColumnList<Row>(props: {
  readonly label: string;
  readonly columns: readonly ListColumn<Row>[];
  readonly rows: readonly Row[];
  readonly rowKey: (row: Row) => string;
  readonly selected?: string | undefined;
  readonly select: (key: string) => void;
  readonly hover?: (key: string | null) => void;
  readonly marks?: (row: Row) => RowMarks;
}): JSX.Element {
  const headerId = useId();
  const ids = props.rows.map(props.rowKey);
  const win = useWindowed({
    count: props.rows.length,
    keyOf: index => ids[index] ?? String(index),
    estimate: SIZE.row,
    follow: false,
  });
  const revealed = useRef<string | undefined>(undefined);
  const scroller = (element: HTMLElement | null): void => {
    win.scroller(element);
    // Called after every commit: brings a newly selected row into view once.
    if (element === null || revealed.current === props.selected) return;
    revealed.current = props.selected;
    const at = props.selected === undefined ? -1 : ids.indexOf(props.selected);
    if (at >= 0) win.reveal(at, () => undefined);
  };
  const selectedIndex = props.selected === undefined ? -1 : ids.indexOf(props.selected);
  const drawn = selectedIndex >= win.first && selectedIndex < win.last;
  const focusSelected = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target !== event.currentTarget) return;
    win.reveal(selectedIndex >= 0 ? selectedIndex : 0, row => row.focus());
  };
  const tracksOf = (columns: readonly ListColumn<Row>[], narrow: boolean): string =>
    columns.map(column => TRACKS[(narrow && column.narrow) || column.size]).join(' ');
  const tracks = {
    '--columns': tracksOf(props.columns, false),
    '--columns-narrow': tracksOf(
      props.columns.filter(column => column.optional !== true),
      true,
    ),
  } as CSSProperties;
  return (
    <div className="vs-columnlist" style={tracks}>
      <Header columns={props.columns} id={headerId} />
      <div
        ref={scroller}
        className="vs-columnlist-rows"
        role="listbox"
        aria-label={props.label}
        aria-describedby={headerId}
        tabIndex={drawn ? -1 : 0}
        onFocus={focusSelected}
        onScroll={win.scrolled}
        onKeyDown={event => navigationKeys(event, win, props.rows.length)}
      >
        <div aria-hidden="true" style={{height: win.before}} />
        {props.rows.slice(win.first, win.last).map((row, offset) => {
          const at = win.first + offset;
          const key = ids[at] ?? String(at);
          return (
            <InSlot key={key} slot={win.slotOf(at)}>
              <Row
                id={key}
                row={row}
                columns={props.columns}
                selected={key === props.selected}
                marks={props.marks?.(row) ?? {}}
                select={() => props.select(key)}
                hover={props.hover === undefined ? undefined : on => props.hover?.(on ? key : null)}
              />
            </InSlot>
          );
        })}
        <div aria-hidden="true" style={{height: win.after}} />
      </div>
    </div>
  );
}

/** A cell's two lines: the figure and a quieter line under it (a status and its cause). */
export function CellLines(props: {
  readonly main: ReactNode;
  readonly note?: string | null | undefined;
}): JSX.Element {
  return (
    <span className="vs-columnlist-lines">
      {props.main}
      {props.note === undefined || props.note === null ? null : (
        <Text variant="small" tone="muted">
          {props.note}
        </Text>
      )}
    </span>
  );
}

/** A cell's link: a word that acts on its own, without picking the row (a row's parent). */
export function CellLink(props: {readonly label: string; readonly press: () => void}): JSX.Element {
  return (
    <button
      type="button"
      className="vs-columnlist-link"
      title={props.label}
      tabIndex={-1}
      onClick={props.press}
    >
      {props.label}
    </button>
  );
}

/** A cell's parts in a line, a small gap apart (a mark and its word, a name and its tag). */
export function CellInline(props: {readonly children: ReactNode}): JSX.Element {
  return <span className="vs-columnlist-inline">{props.children}</span>;
}

/** A small mark before a cell's figure: filled when `on` (it set a best), else an empty slot of its width. */
export function CellMark(props: {readonly on: boolean}): JSX.Element {
  return <span className="vs-columnlist-mark" data-on={props.on || undefined} aria-hidden="true" />;
}
