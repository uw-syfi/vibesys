import type {FocusEvent, JSX, KeyboardEvent} from 'react';
import {SIZE} from '../tokens/index.js';
import {Button} from './button.js';
import type {StyleWithVars} from './css-vars.js';
import {type Guide, Guides} from './guides.js';
import {placeLabels, useInlineSize} from './label-fit.js';
import {navigationKeys, pickKeys} from './listbox.js';
import {Text} from './text.js';
import {useWindowed} from './windowing.js';

/**
 * One piece of a Gantt bar: its span on the axis and its `kind`, a shade (solid, hatched, or
 * grey), not words. A solid or hatched piece is the accent, or the palette's color number
 * `series` (`--series-N`) when it has one; a hatched piece with none is muted.
 */
export interface GanttSegment {
  readonly id: string;
  readonly start: number;
  readonly end: number;
  readonly kind: 'solid' | 'hatched' | 'grey';
  readonly series?: number;
  /** The one-line tooltip. */
  readonly label: string;
  /** Opens this piece. Pointer only: the row's own key opens the row, whose pieces are a list. */
  readonly press?: () => void;
}

/**
 * One row of a `Gantt`: a name (with lineage `guides`), a bar from `start` to `end` split into
 * `segments`, an `outcome` that colors the bar's end (`live` pulses), and a `figure` after it.
 */
export interface GanttRow {
  readonly id: string;
  readonly label: string;
  readonly guides?: readonly Guide[];
  readonly start: number;
  readonly end: number;
  readonly outcome: 'ok' | 'err' | 'live' | 'muted';
  readonly figure?: string;
  /** The figure in full, as the hover shows it, when the figure is a short form. */
  readonly figureTitle?: string;
  readonly segments: readonly GanttSegment[];
  readonly selected?: boolean;
  readonly fresh?: boolean;
  readonly press: () => void;
}

/** A labelled point of the axis. */
export interface GanttTick {
  readonly at: number;
  readonly label: string;
}

/** The part of the axis in view. */
export interface GanttView {
  readonly from: number;
  readonly to: number;
}

/** More rows than this are drawn as plain bars, without their pieces, unless `detail` says otherwise. */
const AGGREGATE_ABOVE = 120;
/** The narrowest view, as a share of the whole axis. */
const MIN_VIEW = 0.01;
/** One zoom step: the view shrinks to this share of itself, or grows by its inverse. */
const ZOOM_STEP = 0.5;
/** One pan step, as a share of the view. */
const PAN_STEP = 0.25;

function percent(share: number): string {
  return `${(share * 100).toFixed(2)}%`;
}

/** Where `value` falls on the axis from `from` to `to`, as a share of it (outside 0 to 1 when outside it). */
function shareOf(value: number, from: number, to: number): number {
  if (to <= from) return 0;
  return (value - from) / (to - from);
}

/** `view` moved, not resized, so that it lies within `whole`. */
function fitted(view: GanttView, whole: GanttView): GanttView {
  const width = view.to - view.from;
  const from = Math.max(whole.from, Math.min(whole.to - width, view.from));
  return {from, to: from + width};
}

/** `view` zoomed by `factor` about its middle, within the whole axis. */
function zoomed(view: GanttView, whole: GanttView, factor: number): GanttView {
  const length = whole.to - whole.from;
  const width = Math.max(length * MIN_VIEW, Math.min(length, (view.to - view.from) * factor));
  const middle = (view.from + view.to) / 2;
  return fitted({from: middle - width / 2, to: middle + width / 2}, whole);
}

/** `view` moved by `share` of its width, within the whole axis. */
function panned(view: GanttView, whole: GanttView, share: number): GanttView {
  const shift = (view.to - view.from) * share;
  return fitted({from: view.from + shift, to: view.to + shift}, whole);
}

/** The view a zoom or pan key asks for, or null for any other key. */
function viewKey(key: string, view: GanttView, whole: GanttView): GanttView | null {
  switch (key) {
    case '+':
    case '=':
      return zoomed(view, whole, ZOOM_STEP);
    case '-':
      return zoomed(view, whole, 1 / ZOOM_STEP);
    case 'ArrowLeft':
      return panned(view, whole, -PAN_STEP);
    case 'ArrowRight':
      return panned(view, whole, PAN_STEP);
    case '0':
      return whole;
    default:
      return null;
  }
}

function GanttBar(props: {
  readonly row: GanttRow;
  readonly view: GanttView;
  readonly aggregate: boolean;
}): JSX.Element {
  const {row, view} = props;
  const start = shareOf(row.start, view.from, view.to);
  const end = shareOf(row.end, view.from, view.to);
  const place: StyleWithVars = {
    '--gantt-from': percent(start),
    '--gantt-span': percent(Math.max(0, end - start)),
  };
  return (
    <span className="vs-gantt-bar" data-outcome={row.outcome} style={place}>
      {props.aggregate
        ? null
        : row.segments.map(segment => {
            const within = (value: number): number =>
              Math.max(0, Math.min(1, shareOf(value, row.start, row.end)));
            const seg: StyleWithVars = {
              '--seg-from': percent(within(segment.start)),
              '--seg-span': percent(Math.max(0, within(segment.end) - within(segment.start))),
              ...(segment.series === undefined
                ? {}
                : {'--seg-color': `var(--series-${segment.series})`}),
            };
            const press = segment.press;
            return (
              // biome-ignore lint/a11y/useKeyWithClickEvents: the row is the keyboard target; a segment is a pointer shortcut to a piece that a list of the row's pieces also reaches by keyboard.
              // biome-ignore lint/a11y/noStaticElementInteractions: see above; the segment carries its meaning in `title` and the row's accessible name.
              <span
                key={segment.id}
                className="vs-gantt-seg"
                data-kind={segment.kind}
                title={segment.label}
                style={seg}
                onClick={
                  press === undefined
                    ? undefined
                    : event => {
                        event.stopPropagation();
                        press();
                      }
                }
              />
            );
          })}
    </span>
  );
}

function AxisRow(props: {
  readonly ticks: readonly GanttTick[];
  readonly view: GanttView;
  readonly nameLabel: string | undefined;
  readonly figureLabel: string | undefined;
}): JSX.Element {
  const {view} = props;
  const [measure, width] = useInlineSize();
  const shown = props.ticks.filter(tick => tick.at >= view.from && tick.at <= view.to);
  // Once the track is measured only the labels that can be read side by side are drawn.
  const placed =
    width === null
      ? null
      : placeLabels(
          shown.map(tick => ({share: shareOf(tick.at, view.from, view.to), text: tick.label})),
          width,
        );
  return (
    <div className="vs-gantt-row" data-axis="" aria-hidden="true">
      <span className="vs-gantt-name vs-listheader" title={props.nameLabel}>
        {props.nameLabel ?? ''}
      </span>
      <span ref={measure} className="vs-gantt-track">
        {(placed ?? shown.map((_, index) => ({index, left: null}))).map(({index, left}) => {
          const tick = shown[index];
          if (tick === undefined) return null;
          const place: StyleWithVars =
            left === null
              ? {'--gantt-from': percent(shareOf(tick.at, view.from, view.to))}
              : {'--tick-left': `${left}px`};
          return (
            <span
              key={tick.at}
              className="vs-gantt-tick"
              data-placed={left === null ? undefined : ''}
              style={place}
            >
              {tick.label}
            </span>
          );
        })}
      </span>
      <span className="vs-gantt-figure vs-listheader" title={props.figureLabel}>
        {props.figureLabel ?? ''}
      </span>
    </div>
  );
}

function sameView(a: GanttView, b: GanttView): boolean {
  const near = (x: number, y: number): boolean =>
    Math.abs(x - y) <= 1e-9 * Math.max(1, Math.abs(y));
  return near(a.from, b.from) && near(a.to, b.to);
}

/** The zoom and pan buttons; one that would not move the view is disabled, with why. */
function ZoomBar(props: {
  readonly view: GanttView;
  readonly whole: GanttView;
  readonly label: string | undefined;
  readonly change: (view: GanttView) => void;
}): JSX.Element {
  const {view, whole, change} = props;
  const step = (text: string, next: GanttView, reason: string): JSX.Element =>
    sameView(next, view) ? (
      <Button
        size="sm"
        variant="quiet"
        text={text}
        disabledReason={reason}
        quietReason
        press={() => undefined}
      />
    ) : (
      <Button size="sm" variant="quiet" text={text} press={() => change(next)} />
    );
  return (
    <div className="vs-gantt-zoom" role="toolbar" aria-label="Time range">
      {step('Zoom in', zoomed(view, whole, ZOOM_STEP), 'Zoomed in as far as it goes')}
      {step('Zoom out', zoomed(view, whole, 1 / ZOOM_STEP), 'The whole range is in view')}
      {step('Earlier', panned(view, whole, -PAN_STEP), 'The start is in view')}
      {step('Later', panned(view, whole, PAN_STEP), 'The end is in view')}
      {step('Whole range', whole, 'The whole range is in view')}
      {props.label === undefined ? null : (
        <span className="vs-gantt-view" role="status">
          <Text variant="small" tone="muted">
            {props.label}
          </Text>
        </span>
      )}
    </div>
  );
}

/**
 * Rows of bars on one time axis from `from` to `to`, a tick row above them, and a `playhead` line
 * through every row. Rows are the options of a windowed listbox (only the rows near the visible
 * part are drawn; Up and Down, Page Up and Down, Home and End move across all of them, Enter and
 * Space press; one tab stop, the selected row or the first). Pressing a segment opens that
 * segment. `strip` drops the names and ticks: one thin row under a heading that already names it.
 *
 * Zoom and pan: `view` is the part of the axis in view (the whole axis unless given). With
 * `changeView` the Gantt offers Zoom in and out, Earlier and Later, and the keys + - 0 and the
 * Left and Right arrows do the same; bars clip at the edges of the view. A button that would not
 * move the view is disabled. `viewLabel` says in words what is in view, beside the buttons, and is
 * announced as it changes. Many rows read as plain
 * bars, without their pieces (`detail` `aggregate`, the default above 120 rows); zoom in to the
 * rows that matter, or pass `detail` `full`.
 */
export function Gantt(props: {
  readonly label: string;
  readonly from: number;
  readonly to: number;
  readonly playhead?: number;
  readonly ticks?: readonly GanttTick[];
  readonly rows: readonly GanttRow[];
  readonly strip?: boolean;
  /** Column names on the tick row: over the names and over the figures. */
  readonly nameLabel?: string;
  readonly figureLabel?: string;
  readonly view?: GanttView;
  readonly changeView?: (view: GanttView) => void;
  /** The part in view in words (`7m to 22m`), shown with the zoom buttons. */
  readonly viewLabel?: string;
  readonly detail?: 'full' | 'aggregate';
  /** `fill` takes the height of its container; the default is at most a thread's. */
  readonly size?: 'default' | 'fill';
}): JSX.Element {
  const whole: GanttView = {from: props.from, to: props.to};
  const view = props.view ?? whole;
  const strip = props.strip === true;
  const {rows, changeView} = props;
  const detail = props.detail ?? (rows.length > AGGREGATE_ABOVE ? 'aggregate' : 'full');
  const win = useWindowed({
    count: rows.length,
    keyOf: index => rows[index]?.id ?? String(index),
    estimate: SIZE['row-dense'],
    follow: false,
  });
  const playhead: StyleWithVars | null =
    props.playhead === undefined
      ? null
      : {'--gantt-from': percent(shareOf(props.playhead, view.from, view.to))};
  const selectedIndex = rows.findIndex(row => row.selected === true);
  const stop = Math.max(0, selectedIndex);
  const drawn = stop >= win.first && stop < win.last;
  const focusStop = (event: FocusEvent<HTMLDivElement>): void => {
    if (event.target === event.currentTarget) win.reveal(stop, row => row.focus());
  };
  const keys = (event: KeyboardEvent<HTMLElement>): void => {
    navigationKeys(event, win, rows.length);
    if (changeView === undefined || event.defaultPrevented) return;
    const next = viewKey(event.key, view, whole);
    if (next === null) return;
    event.preventDefault();
    changeView(next);
  };
  return (
    <div className="vs-gantt" data-strip={strip || undefined} data-size={props.size ?? 'default'}>
      {strip || changeView === undefined ? null : (
        <ZoomBar view={view} whole={whole} label={props.viewLabel} change={changeView} />
      )}
      {strip || props.ticks === undefined ? null : (
        <AxisRow
          ticks={props.ticks}
          view={view}
          nameLabel={props.nameLabel}
          figureLabel={props.figureLabel}
        />
      )}
      <div
        ref={win.scroller}
        className="vs-gantt-rows"
        role="listbox"
        aria-label={props.label}
        tabIndex={drawn ? -1 : 0}
        onFocus={focusStop}
        onScroll={win.scrolled}
        onKeyDown={keys}
      >
        <div aria-hidden="true" style={{height: win.before}} />
        {rows.slice(win.first, win.last).map((row, offset) => (
          <div
            key={row.id}
            className="vs-gantt-row"
            role="option"
            aria-label={row.label}
            aria-posinset={win.first + offset + 1}
            aria-setsize={rows.length}
            tabIndex={win.first + offset === stop ? 0 : -1}
            aria-selected={row.selected === true}
            data-fresh={row.fresh === true || undefined}
            onClick={row.press}
            onKeyDown={event => pickKeys(event, row.press)}
          >
            {strip ? null : (
              <span className="vs-gantt-name" title={row.label}>
                <Guides guides={row.guides} />
                <Text variant="code">{row.label}</Text>
              </span>
            )}
            <span className="vs-gantt-track">
              <GanttBar row={row} view={view} aggregate={detail === 'aggregate'} />
              {playhead === null ? null : <span className="vs-gantt-playhead" style={playhead} />}
            </span>
            <span className="vs-gantt-figure" data-outcome={row.outcome} title={row.figureTitle}>
              {row.figure ?? ''}
            </span>
          </div>
        ))}
        <div aria-hidden="true" style={{height: win.after}} />
      </div>
    </div>
  );
}
