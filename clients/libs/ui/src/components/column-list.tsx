import {
  type CSSProperties,
  type FocusEvent,
  type JSX,
  type ReactNode,
  useCallback,
  useId,
  useRef,
} from 'react';
import {SIZE} from '../tokens/index.js';
import {type ColumnSize, columnTier, tracksOf} from './column-tracks.js';
import {Icon} from './icon.js';
import {useInlineSize} from './label-fit.js';
import {navigationKeys, pickKeys} from './listbox.js';
import {Text} from './text.js';
import {InSlot, useSlot, useWindowed} from './windowing.js';

/** One column of a `ColumnList`: its header, how a row fills its cell, and how the header sorts. */
export type {ColumnSize};
export interface ListColumn<Row> {
  readonly key: string;
  readonly header: string;
  readonly size: ColumnSize;
  readonly align?: 'start' | 'end';
  /** Drawn only while the list has room: it gives way when the list is narrow. */
  readonly optional?: boolean;
  /**
   * Pressing the `CellLink` word of the column's cell acts on its own (a link to another row) and
   * picks nothing; it says whether it did, and a press it did not take, or one anywhere else in the
   * cell, picks the row. The row holds the press: a cell has no control of its own inside the option.
   */
  readonly act?: (row: Row) => boolean;
  /** The cell's text wraps onto more lines instead of being cut short. */
  readonly wrap?: boolean;
  /** The column's size when the list is narrow; the same by default. */
  readonly narrow?: ColumnSize;
  /** The column's size when a crowded list is tighter still; its `narrow` size by default. */
  readonly tight?: ColumnSize;
  /** What the pointer shows over a cell whole, for a cell that may be cut short. */
  readonly hint?: (row: Row) => string | undefined;
  /** A small mark after the header (`+2`) and what it stands for, on hover. */
  readonly note?: {readonly label: string; readonly hint: string};
  readonly cell: (row: Row) => ReactNode;
  /**
   * Pressing the header sorts by the column. `marked` draws the arrow on the column the list is
   * sorted by, whose header is a label: pressing it would change nothing.
   */
  readonly sort?: {readonly marked: boolean; readonly press: () => void};
}

/** What a row of a `ColumnList` is marked as, beside whether it is the selected one. */
export interface RowMarks {
  /** On the path that leads to the selected row: a bar in the accent. */
  readonly lineage?: boolean;
  /** Changed while on screen: the row flashes once. */
  readonly fresh?: boolean;
}

/**
 * A callback ref that calls `changed` whenever the element's width changes after the first
 * measure, with the latest `changed` of the render (no effect hook: the observer lives and dies with
 * the element).
 */
function useWidthWatch(
  changed: () => void,
): (element: HTMLElement | null) => (() => void) | undefined {
  const latest = useRef(changed);
  latest.current = changed;
  return useCallback((element: HTMLElement | null) => {
    if (element === null || typeof ResizeObserver === 'undefined') return undefined;
    let width = element.getBoundingClientRect().width;
    const watch = new ResizeObserver(entries => {
      const next = entries.at(-1)?.contentRect.width;
      if (next === undefined || Math.abs(next - width) < 1) return;
      width = next;
      latest.current();
    });
    watch.observe(element);
    return () => watch.disconnect();
  }, []);
}

function Header<Row>(props: {
  readonly columns: readonly ListColumn<Row>[];
  readonly id: string;
  readonly watch: (element: HTMLElement | null) => (() => void) | undefined;
}): JSX.Element {
  return (
    <div className="vs-columnlist-head" id={props.id} ref={props.watch}>
      {props.columns.map(column => (
        <span
          key={column.key}
          className="vs-columnlist-headcell"
          data-align={column.align}
          data-optional={column.optional || undefined}
          data-marked={column.sort?.marked || undefined}
          data-tight-blank={column.tight === 'dot' || undefined}
          title={column.header}
        >
          {column.sort === undefined || column.sort.marked ? (
            <span className="vs-columnlist-marked">
              {column.header}
              {column.sort?.marked ? <Icon name="chevron-down" /> : null}
            </span>
          ) : (
            <button type="button" onClick={column.sort.press} title={`Sort by ${column.header}`}>
              {column.header}
            </button>
          )}
          {column.note === undefined ? null : (
            <span className="vs-columnlist-note" title={column.note.hint}>
              {column.note.label}
            </span>
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
  readonly describe: string | undefined;
  readonly select: () => void;
  readonly hover: ((on: boolean) => void) | undefined;
}): JSX.Element {
  const slot = useSlot();
  const {hover} = props;
  const nameId = useId();
  return (
    <div
      className="vs-columnlist-row"
      ref={slot?.measure}
      role="option"
      aria-labelledby={nameId}
      tabIndex={props.selected ? 0 : -1}
      aria-selected={props.selected}
      aria-description={props.describe}
      aria-posinset={slot === null ? undefined : slot.index + 1}
      aria-setsize={slot === null ? undefined : slot.size}
      data-lineage={props.marks.lineage || undefined}
      data-fresh={props.marks.fresh || undefined}
      onClick={event => {
        // Only the link's own word acts; the rest of its cell, like the rest of the row, picks the row.
        const target = event.target instanceof Element ? event.target : null;
        const cell = target?.closest('[data-column]') ?? null;
        const column = props.columns.find(each => each.key === cell?.getAttribute('data-column'));
        const onLink = target?.closest('.vs-columnlist-link') != null;
        if (!onLink || column?.act?.(props.row) !== true) props.select();
      }}
      onMouseEnter={hover === undefined ? undefined : () => hover(true)}
      onMouseLeave={hover === undefined ? undefined : () => hover(false)}
      onFocus={hover === undefined ? undefined : () => hover(true)}
      onBlur={hover === undefined ? undefined : () => hover(false)}
      onKeyDown={event => {
        if (event.target === event.currentTarget) pickKeys(event, props.select);
      }}
    >
      {props.columns.map((column, at) => (
        <span
          key={column.key}
          id={at === 0 ? nameId : undefined}
          className="vs-columnlist-cell"
          data-column={column.key}
          data-align={column.align}
          data-optional={column.optional || undefined}
          data-wrap={column.wrap || undefined}
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
 * A row is named by its first column, as a screen reader hears it, and `describe` says the rest, so
 * a cell that names another row (a link to the row it builds on) does not rename this one.
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
  /** What the row says besides its name, whole, for assistive technology: a cell may show it cut short or compact. */
  readonly describe?: (row: Row) => string | undefined;
  /**
   * Too many columns for the room a narrow list has: below its tight width the columns take their
   * `tight` size and the parts of a cell marked `data-tight-hide` give way (a status word beside
   * its dot).
   */
  readonly crowded?: boolean;
}): JSX.Element {
  const headerId = useId();
  const ids = props.rows.map(props.rowKey);
  const win = useWindowed({
    count: props.rows.length,
    keyOf: index => ids[index] ?? String(index),
    estimate: SIZE.row,
    follow: false,
  });
  const selectedIndex = props.selected === undefined ? -1 : ids.indexOf(props.selected);
  const drawn = selectedIndex >= win.first && selectedIndex < win.last;
  const focusSelected = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target !== event.currentTarget) return;
    win.reveal(selectedIndex >= 0 ? selectedIndex : 0, row => row.focus());
  };
  const [measure, width] = useInlineSize();
  // A width that changes rewraps the rows: the picked row stays in view, as an idea opened beside the
  // list narrows it.
  const watch = useWidthWatch(() => {
    if (selectedIndex >= 0) win.reveal(selectedIndex, () => undefined);
  });
  const tier = columnTier(width, props.columns, props.crowded === true);
  const tracks = {
    '--columns': tracksOf(props.columns, 'wide'),
    '--columns-narrow': tracksOf(props.columns, 'narrow'),
    '--columns-tight': tracksOf(props.columns, 'tight'),
  } as CSSProperties;
  return (
    <div className="vs-columnlist" ref={measure} style={tracks} data-tier={tier}>
      <Header columns={props.columns} id={headerId} watch={watch} />
      <div
        ref={win.scroller}
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
                describe={props.describe?.(row)}
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

/**
 * A cell's word that acts on its own (the column's `act`), drawn as a link: quiet, and in the accent
 * while `lit` (its row is on the path to the selected row).
 */
export function CellLink(props: {readonly label: string; readonly lit?: boolean}): JSX.Element {
  return (
    <span className="vs-columnlist-link" data-lit={props.lit || undefined} title={props.label}>
      {props.label}
    </span>
  );
}

/** A cell's parts in a line, a small gap apart (a mark and its word, a name and its tag). */
export function CellInline(props: {
  readonly children: ReactNode;
  /** The parts stay on one line, cut short (with the cell's hint) when there is no room. */
  readonly nowrap?: boolean;
}): JSX.Element {
  return (
    <span className="vs-columnlist-inline" data-nowrap={props.nowrap || undefined}>
      {props.children}
    </span>
  );
}

/** A small mark before a cell's figure: filled when `on` (it set a best), else an empty slot of its width. */
export function CellMark(props: {readonly on: boolean}): JSX.Element {
  return <span className="vs-columnlist-mark" data-on={props.on || undefined} aria-hidden="true" />;
}
