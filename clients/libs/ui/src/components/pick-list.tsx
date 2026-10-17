import {
  Children,
  createContext,
  type FocusEvent,
  isValidElement,
  type JSX,
  type ReactNode,
  useContext,
  useId,
} from 'react';
import {SIZE} from '../tokens/index.js';
import {type Guide, Guides} from './guides.js';
import {navigationKeys, pickKeys} from './listbox.js';
import {Meter} from './meter.js';
import {Chip, StatusDot, type StatusTone} from './status.js';
import {Swatch} from './swatch-legend.js';
import {Text} from './text.js';
import {InSlot, useSlot, useWindowed} from './windowing.js';

const SelectedRow = createContext<string | undefined>(undefined);

/** One trailing column of a list's header: its name, `bar` wide as a row's bar. */
export interface HeaderColumn {
  readonly label: string;
  /** As wide as a row's bar. */
  readonly bar?: boolean;
  /** As wide as a row's figure, or as a row's trail. */
  readonly fixed?: 'figure' | 'trail';
}

/** The one-line caption of a list's columns: `start` over the names, `end` over the trailing columns. */
export interface ListHeader {
  readonly start: string;
  readonly end?: readonly HeaderColumn[];
}

function Header(props: {readonly header: ListHeader; readonly id: string}): JSX.Element {
  return (
    <div className="vs-listheader" id={props.id}>
      <span className="vs-listheader-start">{props.header.start}</span>
      {(props.header.end ?? []).map(column => (
        <span
          key={column.label}
          className="vs-listheader-col"
          data-bar={column.bar === true || undefined}
          data-fixed={column.fixed}
        >
          {column.label}
        </span>
      ))}
    </div>
  );
}

/** The id of a row element: its `id` prop, or its key. */
function idOf(row: ReactNode, index: number): string {
  if (!isValidElement(row)) return String(index);
  const id = (row.props as {readonly id?: unknown}).id;
  return typeof id === 'string' ? id : String(row.key ?? index);
}

/**
 * A list of `PickRow`s to choose from: a listbox with one tab stop, the `selected` row's id, or
 * the list itself, which passes the focus to that row (or the first drawn). It scrolls inside
 * itself (`size` `fill` takes the height of its container, the default at most a thread's) and
 * draws only the rows near the visible part, between spacers as tall as the rest, so any number
 * of rows costs a screenful. The keys reach every row, drawn or not: Up and Down step, Page Up
 * and Down move a screenful, Home and End jump to the ends, Enter and Space pick. `header` is an
 * optional small line over the rows naming their columns.
 */
export function PickList(props: {
  readonly label: string;
  readonly selected?: string;
  readonly header?: ListHeader;
  readonly size?: 'default' | 'fill';
  readonly children: ReactNode;
}): JSX.Element {
  const headerId = useId();
  const rows = Children.toArray(props.children);
  const ids = rows.map(idOf);
  const win = useWindowed({
    count: rows.length,
    keyOf: index => ids[index] ?? String(index),
    estimate: SIZE.row,
    follow: false,
  });
  const selectedIndex = props.selected === undefined ? -1 : ids.indexOf(props.selected);
  const drawn = selectedIndex >= win.first && selectedIndex < win.last;
  const focusSelected = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target !== event.currentTarget) return;
    if (selectedIndex >= 0) win.reveal(selectedIndex, row => row.focus());
    else win.reveal(0, row => row.focus());
  };
  return (
    <SelectedRow.Provider value={props.selected}>
      <div className="vs-picklist" data-size={props.size ?? 'default'}>
        {props.header === undefined ? null : <Header header={props.header} id={headerId} />}
        <div
          ref={win.scroller}
          className="vs-picklist-rows"
          role="listbox"
          aria-label={props.label}
          aria-describedby={props.header === undefined ? undefined : headerId}
          tabIndex={drawn ? -1 : 0}
          onFocus={focusSelected}
          onScroll={win.scrolled}
          onKeyDown={event => navigationKeys(event, win, rows.length)}
        >
          <div aria-hidden="true" style={{height: win.before}} />
          {rows.slice(win.first, win.last).map((row, offset) => (
            <InSlot key={ids[win.first + offset]} slot={win.slotOf(win.first + offset)}>
              {row}
            </InSlot>
          ))}
          <div aria-hidden="true" style={{height: win.after}} />
        </div>
      </div>
    </SelectedRow.Provider>
  );
}

/** The share of a row's `bar`, in a tone. */
export interface RowBar {
  readonly value: number;
  readonly tone: 'accent' | 'ok' | 'muted';
  readonly label: string;
}

/** What follows a row's text: its badges, its bar, its figure and its trail. */
function Trail(props: {
  readonly badges?: readonly {readonly label: string; readonly tone: StatusTone}[];
  readonly bar?: RowBar;
  readonly figure?: string;
  readonly figureTone?: 'default' | 'ok' | 'err' | 'muted';
  readonly trail?: {readonly label: string; readonly press?: () => void};
}): JSX.Element {
  const trail = props.trail;
  return (
    <>
      {(props.badges ?? []).map(badge => (
        <Chip key={badge.label} label={badge.label} tone={badge.tone} />
      ))}
      {props.bar === undefined ? null : (
        <span className="vs-pickrow-bar">
          <Meter label={props.bar.label} value={props.bar.value} tone={props.bar.tone} />
        </span>
      )}
      {props.figure === undefined ? null : (
        <span className="vs-pickrow-figure" data-tone={props.figureTone ?? 'default'}>
          {props.figure}
        </span>
      )}
      {trail === undefined ? null : trail.press === undefined ? (
        <span className="vs-pickrow-trail">{trail.label}</span>
      ) : (
        <button
          type="button"
          className="vs-pickrow-trail"
          tabIndex={-1}
          onClick={event => {
            event.stopPropagation();
            trail.press?.();
          }}
        >
          {trail.label}
        </button>
      )}
    </>
  );
}

/**
 * One row of a `PickList`, one line (28 px in a compact screen): what it is (`title`), its most
 * important line (`summary`, which gives way first when the row is narrow), `badges`, a `bar`, a
 * trailing `figure` (a gain, a duration) in a tone, and its `state` (a dot that names the state on
 * hover and when the row has the focus). `guides` draw a lineage tree before the row, one cell per ancestor level, and `lit`
 * marks the cells on the path the reader follows. `fresh` flashes the row once (it changed while
 * on screen). The row whose id is the list's `selected` keeps a bar and a fill.
 */
export function PickRow(props: {
  readonly id: string;
  readonly title: string;
  readonly summary?: string;
  readonly figure?: string;
  readonly figureTone?: 'default' | 'ok' | 'err' | 'muted';
  readonly lead?: string;
  readonly state?: {readonly tone: StatusTone; readonly label: string};
  readonly fresh?: boolean;
  readonly mono?: boolean;
  readonly guides?: readonly Guide[];
  /** On the path from the selected or pointed-at row back to the root: guides in accent, a faint fill. */
  readonly path?: boolean;
  /** Called when the pointer or the focus enters (true) or leaves (false) the row. */
  readonly hover?: (on: boolean) => void;
  readonly bar?: RowBar;
  /** A muted last column after the figure, such as "from <parent>"; with `press` it is a link. */
  readonly trail?: {readonly label: string; readonly press?: () => void};
  /** An accent stripe down the row's edge: it is on the chain the reader is led along (a winning path). */
  readonly stripe?: boolean;
  /** A square of the palette's color number (`--series-N`) before the title: what the row is, by color. */
  readonly swatch?: number;
  readonly badges?: readonly {readonly label: string; readonly tone: StatusTone}[];
  readonly select: () => void;
}): JSX.Element {
  const selected = props.id === useContext(SelectedRow);
  const slot = useSlot();
  const hover = props.hover;
  return (
    <div
      className="vs-pickrow"
      ref={slot?.measure}
      role="option"
      tabIndex={selected ? 0 : -1}
      aria-selected={selected}
      aria-posinset={slot === null ? undefined : slot.index + 1}
      aria-setsize={slot === null ? undefined : slot.size}
      data-fresh={props.fresh === true || undefined}
      data-path={props.path === true || undefined}
      data-stripe={props.stripe === true || undefined}
      onClick={props.select}
      onMouseEnter={hover === undefined ? undefined : () => hover(true)}
      onMouseLeave={hover === undefined ? undefined : () => hover(false)}
      onFocus={hover === undefined ? undefined : () => hover(true)}
      onBlur={hover === undefined ? undefined : () => hover(false)}
      onKeyDown={event => pickKeys(event, props.select)}
    >
      <Guides guides={props.guides} />
      {props.state === undefined ? null : (
        <span className="vs-pickrow-dot" data-tip={props.state.label}>
          <StatusDot tone={props.state.tone} label={props.state.label} />
        </span>
      )}
      {props.lead === undefined ? null : (
        <span className="vs-pickrow-lead">
          <Text variant="code" tone="muted">
            {props.lead}
          </Text>
        </span>
      )}
      {props.swatch === undefined ? null : <Swatch series={props.swatch} />}
      <span className="vs-pickrow-text">
        <Text variant={props.mono === true ? 'code' : 'body-strong'}>{props.title}</Text>
        {props.summary === undefined ? null : (
          <Text variant="small" tone="muted">
            {props.summary}
          </Text>
        )}
      </span>
      <Trail {...props} />
    </div>
  );
}
