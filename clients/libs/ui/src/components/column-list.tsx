import {
  type ComponentProps,
  type CSSProperties,
  type FocusEvent,
  type JSX,
  type KeyboardEvent,
  type PointerEvent,
  type ReactNode,
  useCallback,
  useId,
  useRef,
  useState,
} from 'react';
import {SIZE, SPACE, TYPE} from '../tokens/index.js';
import {
  type ColumnSize,
  type ColumnWidths,
  clampColumnWidth,
  columnTier,
  MAX_COLUMN_WIDTH,
  MIN_COLUMN_WIDTH,
  tracksOf,
} from './column-tracks.js';
import {Icon} from './icon.js';
import {useInlineSize} from './label-fit.js';
import {navigationKeys, pickKeys} from './listbox.js';
import {ScrollArea} from './scroll-area.js';
import {Sparkline} from './sparkline.js';
import {Text} from './text.js';
import {InSlot, useSlot, useWindowed} from './windowing.js';

/**
 * The height of a row of two lines (a figure and a note under it): the two lines, the row's padding
 * and its hairline borders, from the same tokens the stylesheet takes them from.
 */
const TWO_LINES_ROW = TYPE.body.line + TYPE.small.line + 2 * SPACE['space-3'] + 2 * SIZE.hairline;

/** One column of a `ColumnList`: its header, how a row fills its cell, and how the header sorts. */
export type {ColumnSize};

interface ColumnNote {
  readonly label: string;
  /** A shorter visible label beside the header while narrow; `null` gives way entirely. */
  readonly narrowLabel?: string | null;
  readonly hint: string;
}

export interface ListColumn<Row> {
  readonly key: string;
  readonly header: string;
  /** A concise visible name while the list is narrow; controls keep `header` as their full name. */
  readonly narrowHeader?: string;
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
  readonly note?: ColumnNote;
  readonly cell: (row: Row) => ReactNode;
  /**
   * Pressing the header sorts by the column. `direction` is the way the list is sorted by it, or
   * null while it is not: its arrow is drawn in a slot every sortable header keeps, so a header is as
   * wide sorted as not and pressing it moves nothing.
   */
  readonly sort?:
    | {readonly direction: 'asc' | 'desc' | null; readonly press: () => void}
    | undefined;
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

/** How far an arrow key moves a column's edge, in px. */
const KEY_STEP = 16;

/**
 * The edge of a column's header, a vertical separator to drag (or to move with the arrow keys; a
 * double press puts the column back to its own width). Dragging shows the width as it goes and gives
 * it to `commit` when the pointer is released; a key gives each step.
 */
function Grip(props: {
  readonly header: string;
  readonly width: number;
  readonly dragging: (width: number | null) => void;
  readonly commit: (width: number | null) => void;
}): JSX.Element {
  const start = useRef<{readonly x: number; readonly width: number} | null>(null);
  const move = (event: PointerEvent<HTMLElement>): void => {
    const from = start.current;
    if (from !== null) props.dragging(clampColumnWidth(from.width + event.clientX - from.x));
  };
  const end = (event: PointerEvent<HTMLElement>): void => {
    const from = start.current;
    start.current = null;
    props.dragging(null);
    if (from !== null) props.commit(clampColumnWidth(from.width + event.clientX - from.x));
  };
  const key = (event: KeyboardEvent<HTMLElement>): void => {
    const by = event.key === 'ArrowRight' ? KEY_STEP : event.key === 'ArrowLeft' ? -KEY_STEP : 0;
    if (by !== 0) props.commit(clampColumnWidth(props.width + by));
    else if (event.key === 'Enter' || event.key === 'Escape') props.commit(null);
    else return;
    event.preventDefault();
  };
  return (
    <hr
      className="vs-columnlist-grip"
      aria-orientation="vertical"
      aria-label={`Resize ${props.header}`}
      aria-valuemin={MIN_COLUMN_WIDTH}
      aria-valuemax={MAX_COLUMN_WIDTH}
      aria-valuenow={Math.floor(props.width + 0.5)}
      title={`Resize ${props.header} column (drag or use arrow keys; double-click or press Enter to reset)`}
      tabIndex={0}
      onPointerDown={event => {
        if (event.button !== 0) return;
        start.current = {x: event.clientX, width: props.width};
        event.currentTarget.setPointerCapture?.(event.pointerId);
        event.preventDefault();
      }}
      onPointerMove={move}
      onPointerUp={end}
      onPointerCancel={() => {
        start.current = null;
        props.dragging(null);
      }}
      onDoubleClick={() => props.commit(null)}
      onKeyDown={key}
    />
  );
}

function HeaderNote(props: {readonly note: ColumnNote}): JSX.Element {
  if (props.note.narrowLabel === null) {
    return (
      <span className="vs-columnlist-note" title={props.note.hint} data-wide-only>
        {props.note.label}
      </span>
    );
  }
  return (
    <span className="vs-columnlist-note" title={props.note.hint}>
      {props.note.narrowLabel === undefined ? (
        props.note.label
      ) : (
        <>
          <span data-wide-only>{props.note.label}</span>
          <span data-narrow-only>{props.note.narrowLabel}</span>
        </>
      )}
    </span>
  );
}

function HeaderLabel<Row>(props: {readonly column: ListColumn<Row>}): ReactNode {
  return props.column.narrowHeader === undefined ? (
    props.column.header
  ) : (
    <>
      <span data-wide-only>{props.column.header}</span>
      <span data-narrow-only>{props.column.narrowHeader}</span>
    </>
  );
}

interface HeaderResize {
  readonly dragging: (key: string, width: number | null) => void;
  readonly commit: (key: string, width: number | null) => void;
  readonly widthOf: (key: string) => number;
}

function HeaderCell<Row>(props: {
  readonly column: ListColumn<Row>;
  readonly resize: HeaderResize | undefined;
}): JSX.Element {
  const {column, resize} = props;
  const resizable = resize !== undefined && column.size !== 'fill';
  return (
    <span
      className="vs-columnlist-headcell"
      data-key={column.key}
      data-align={column.align}
      data-optional={column.optional || undefined}
      data-sorted={column.sort?.direction ?? undefined}
      data-sortable={column.sort === undefined ? undefined : true}
      data-resizable={resizable || undefined}
      data-tight-blank={column.tight === 'dot' || undefined}
      title={column.header}
    >
      {column.sort === undefined ? (
        <span className="vs-columnlist-marked">
          <HeaderLabel column={column} />
        </span>
      ) : (
        <button
          type="button"
          onClick={column.sort.press}
          title={`Sort by ${column.header}`}
          aria-label={`Sort by ${column.header}`}
        >
          <HeaderLabel column={column} />
          <span className="vs-columnlist-sortmark" aria-hidden="true">
            {column.sort.direction === null ? '↕' : <Icon name="arrow-up" />}
          </span>
        </button>
      )}
      {column.note === undefined ? null : <HeaderNote note={column.note} />}
      {!resizable || resize === undefined ? null : (
        <Grip
          header={column.header}
          width={resize.widthOf(column.key)}
          dragging={width => resize.dragging(column.key, width)}
          commit={width => resize.commit(column.key, width)}
        />
      )}
    </span>
  );
}

function Header<Row>(props: {
  readonly columns: readonly ListColumn<Row>[];
  readonly id: string;
  readonly watch: (element: HTMLElement | null) => (() => void) | undefined;
  /** Gives columns an edge to drag; the width a column has now, for its edge to start from. */
  readonly resize: HeaderResize | undefined;
}): JSX.Element {
  return (
    <div className="vs-columnlist-head" id={props.id} ref={props.watch}>
      {props.columns.map(column => (
        <HeaderCell key={column.key} column={column} resize={props.resize} />
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

/** What the headers' edges need to drag a column: show its width as it goes, give it, read it now. */
function useGrips(
  resize: ((key: string, width: number | null) => void) | undefined,
  widths: ColumnWidths,
  setDrag: (drag: {readonly key: string; readonly width: number} | null) => void,
  list: {readonly current: HTMLElement | null},
) {
  if (resize === undefined) return undefined;
  return {
    dragging: (key: string, dragged: number | null) =>
      setDrag(dragged === null ? null : {key, width: dragged}),
    commit: (key: string, committed: number | null) => resize(key, committed),
    // A column's width now: the one given it, else what the header draws it at.
    widthOf: (key: string) => {
      const given = widths[key];
      if (given !== undefined) return given;
      const cell = list.current?.querySelector(`.vs-columnlist-head [data-key="${key}"]`);
      return cell?.getBoundingClientRect().width ?? 0;
    },
  };
}

/**
 * A list of rows in columns to choose from: a listbox with one tab stop, whose header names the
 * columns and sorts by the ones that offer it. It scrolls inside itself and draws only the rows near
 * the visible part, so any number of rows costs a screenful (the same windowing as `PickList`). The
 * keys reach every row: Up and Down step, Page Up and Down move a screenful, Home and End jump,
 * Enter and Space pick. `selected` is the picked row's key; `marks` adds a bar to rows on its path.
 * A row is named by its first column, as a screen reader hears it, and `describe` says the rest, so
 * a cell that names another row (a link to the row it builds on) does not rename this one. The rows
 * scroll at most sixteen rows tall (`size` `fill` takes the height of its container, as in a pane).
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
  /** `fill`: the rows take the height the container gives the list; the default is their own, to a cap. */
  readonly size?: 'default' | 'fill';
  /**
   * Rows of two lines (`CellLines`): every row keeps the height of two, so a row with one line is as
   * tall as one with two, and the list knows how tall it is before any row is drawn (nothing below
   * it moves when the rows are measured).
   */
  readonly lines?: 1 | 2;
  /**
   * The widths a person gave columns by dragging their edges, by column key. With `resize`, every
   * column but the one that fills gets an edge to drag; `resize` is told the width a drag, or an arrow
   * key, ends on (px), or null to put the column back to its own.
   */
  readonly widths?: ColumnWidths;
  readonly resize?: (key: string, width: number | null) => void;
}): JSX.Element {
  const headerId = useId();
  // The width a column has while its edge is dragged: shown at once, given away when it is let go.
  const [drag, setDrag] = useState<{readonly key: string; readonly width: number} | null>(null);
  const widths: ColumnWidths =
    drag === null ? (props.widths ?? {}) : {...props.widths, [drag.key]: drag.width};
  const list = useRef<HTMLDivElement | null>(null);
  const ids = props.rows.map(props.rowKey);
  const win = useWindowed({
    count: props.rows.length,
    keyOf: index => ids[index] ?? String(index),
    estimate: props.lines === 2 ? TWO_LINES_ROW : SIZE.row,
    follow: false,
  });
  const selectedIndex = props.selected === undefined ? -1 : ids.indexOf(props.selected);
  const drawn = selectedIndex >= win.first && selectedIndex < win.last;
  const focusSelected = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target !== event.currentTarget) return;
    win.reveal(selectedIndex >= 0 ? selectedIndex : 0, row => row.focus());
  };
  const [measure, width] = useInlineSize();
  const attach = useCallback(
    (element: HTMLDivElement | null) => {
      list.current = element;
      return measure(element);
    },
    [measure],
  );
  // A width that changes rewraps the rows: the picked row stays in view, as an idea opened beside the
  // list narrows it.
  const watch = useWidthWatch(() => {
    if (selectedIndex >= 0) win.reveal(selectedIndex, () => undefined);
  });
  const tracked = props.columns.map(column => ({
    key: column.key,
    size: column.size,
    optional: column.optional,
    narrow: column.narrow,
    tight: column.tight,
  }));
  const tier = columnTier(width, tracked, props.crowded === true, widths);
  const tracks = {
    '--columns': tracksOf(tracked, 'wide', widths),
    '--columns-narrow': tracksOf(tracked, 'narrow', widths),
    '--columns-tight': tracksOf(tracked, 'tight', widths),
  } as CSSProperties;
  const grips = useGrips(props.resize, widths, setDrag, list);
  return (
    <div
      className="vs-columnlist"
      ref={attach}
      style={tracks}
      data-tier={tier}
      data-size={props.size ?? 'default'}
      data-lines={props.lines === 2 ? 2 : undefined}
    >
      <Header columns={props.columns} id={headerId} watch={watch} resize={grips} />
      <ScrollArea
        kind="feed"
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
      </ScrollArea>
    </div>
  );
}

/**
 * A cell's two lines: the figure and a quieter line under it (a status and its cause). The note is
 * text, set small and muted; a note that is more than text (parts that come and go with the list's
 * width) is the caller's own element, which then sets its own face. `mainTail` is a part that follows
 * the figure on its line and is never cut: when the cell is too narrow the figure gives way to it
 * (a part number after a name), with an ellipsis, and the tail stays whole.
 */
export function CellLines(props: {
  readonly main: ReactNode;
  readonly mainTail?: ReactNode;
  readonly note?: ReactNode;
}): JSX.Element {
  return (
    <span className="vs-columnlist-lines">
      {props.mainTail === undefined || props.mainTail === null ? (
        props.main
      ) : (
        <span className="vs-columnlist-split">
          <span className="vs-columnlist-split-lead">{props.main}</span>
          <span className="vs-columnlist-split-tail">{props.mainTail}</span>
        </span>
      )}
      {props.note === undefined || props.note === null ? null : typeof props.note === 'string' ? (
        <Text variant="small" tone="muted">
          {props.note}
        </Text>
      ) : (
        props.note
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

/**
 * A cell's small trend: a `Sparkline` as wide as the cell leaves it and as high as the row's two
 * lines, and after it a slot of fixed width for `more` (how many other series there are, as `+2`),
 * so the lines of every row start and end at the same places whether or not a row has more.
 */
export function CellSparkline(
  props: ComponentProps<typeof Sparkline> & {readonly more?: number},
): JSX.Element {
  const {more, ...spark} = props;
  return (
    <span className="vs-columnlist-spark">
      <Sparkline {...spark} />
      <span className="vs-columnlist-spark-more">
        {more === undefined || more <= 0 ? null : (
          <Text variant="caption" tone="muted">{`+${more}`}</Text>
        )}
      </span>
    </span>
  );
}
