import type {FocusEvent, JSX, KeyboardEvent} from 'react';
import {SIZE} from '../tokens/index.js';
import {Button} from './button.js';
import type {StyleWithVars} from './css-vars.js';
import {useEntering} from './entering.js';
import {type Guide, Guides} from './guides.js';
import {Icon} from './icon.js';
import {placeLabels, useInlineSize} from './label-fit.js';
import {navigationKeys, pickKeys} from './listbox.js';
import {ScrollArea} from './scroll-area.js';
import {Text} from './text.js';
import {fittedTimeTicks, type ZoneOffset} from './time-ticks.js';
import {useWindowed} from './windowing.js';

/**
 * One piece of a Gantt bar: its span on the axis and its `kind`, a shade (solid, hatched, or
 * grey), not words. A solid or hatched piece is the accent, or the palette's color number
 * `series` (`--series-N`) when it has one; a hatched piece with none is muted; `err` is the error color.
 */
export interface GanttSegment {
  readonly id: string;
  readonly start: number;
  readonly end: number;
  readonly kind: 'solid' | 'hatched' | 'grey' | 'err';
  readonly series?: number;
  /** The one-line tooltip. */
  readonly label: string;
  /** Opens this piece. Pointer only: the row's own key opens the row, whose pieces are a list. */
  readonly press?: () => void;
}

/** A thin line across a row's bar at one moment, such as where a result came back. */
export interface GanttMark {
  readonly at: number;
  readonly tone: 'ok' | 'err';
  /** The one-line tooltip. */
  readonly label: string;
}

/** The fold of a row that has rows under it: whether they are shown, and how to change that. */
export interface GanttFold {
  readonly open: boolean;
  readonly toggle: () => void;
}

/**
 * One row of a `Gantt`: a name (with lineage `guides`), a bar from `start` to `end` split into
 * `segments`, an `outcome` that colors the bar's end (`live` pulses), and a `figure` after it.
 * A row may carry `marks` on its bar, a `tag` in its own column (see `tagLabel`), a `fold` (a
 * chevron before the name; Right opens it and Left closes it) and a `count`, the number of rows
 * folded into it, which is drawn as a stack under the bar and as `×count` after the name.
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
  readonly marks?: readonly GanttMark[];
  readonly tag?: string;
  readonly fold?: GanttFold;
  readonly count?: number;
  readonly selected?: boolean;
  readonly fresh?: boolean;
  readonly press: () => void;
}

/** A labelled point of the axis. */
export interface GanttTick {
  readonly at: number;
  readonly label: string;
}

/**
 * A time axis the Gantt ticks itself, at even, tidy intervals that fit its width, labelled by `label`.
 * Ticks of an hour or more fall on whole hours and days of the clock whose offset from UTC `zone`
 * tells (UTC when it is not given), the clock `label` writes in.
 */
export interface GanttAxis {
  readonly label: (at: number, spanMs: number) => string;
  readonly zone?: ZoneOffset;
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
    <span
      className="vs-gantt-bar"
      data-outcome={row.outcome}
      data-stacked={((row.count ?? 0) > 0 && row.fold?.open !== true) || undefined}
      style={place}
    >
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
      {(row.marks ?? []).map(mark => {
        const at = shareOf(mark.at, row.start, row.end);
        const place: StyleWithVars = {'--mark-at': percent(Math.max(0, Math.min(1, at)))};
        return (
          <span
            key={`${mark.at}-${mark.label}`}
            className="vs-gantt-mark"
            data-tone={mark.tone}
            title={mark.label}
            style={place}
          />
        );
      })}
    </span>
  );
}

/** The axis a Gantt draws: its given ticks, else its time axis, else none. */
function axisOf(props: {readonly ticks?: readonly GanttTick[]; readonly axis?: GanttAxis}) {
  return props.ticks ?? props.axis;
}

function AxisRow(props: {
  readonly ticks: readonly GanttTick[] | GanttAxis;
  readonly view: GanttView;
  readonly nameLabel: string | undefined;
  readonly tagLabel: string | undefined;
  readonly figureLabel: string | undefined;
}): JSX.Element {
  const {view} = props;
  const [measure, width] = useInlineSize();
  // A time axis draws each tick centred on its moment, at the width it has. Given ticks are
  // filtered to the view and, once the track is measured, only those that can be read side by side stay.
  const ticks = props.ticks;
  const timed = 'label' in ticks;
  const shown = timed
    ? fittedTimeTicks(view.from, view.to, width, ticks.label, ticks.zone)
    : ticks.filter(tick => tick.at >= view.from && tick.at <= view.to);
  const placed =
    width === null || timed
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
      {props.tagLabel === undefined ? null : (
        <span className="vs-gantt-tag vs-listheader" title={props.tagLabel}>
          {props.tagLabel}
        </span>
      )}
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
              data-centred={timed || undefined}
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

function ZoomBar(props: {
  readonly view: GanttView;
  readonly whole: GanttView;
  readonly change: (view: GanttView) => void;
}): JSX.Element {
  const {view, whole, change} = props;
  return (
    <div className="vs-gantt-zoom" role="toolbar" aria-label="Time range">
      <Button
        size="sm"
        variant="quiet"
        text="Zoom in"
        press={() => change(zoomed(view, whole, ZOOM_STEP))}
      />
      <Button
        size="sm"
        variant="quiet"
        text="Zoom out"
        press={() => change(zoomed(view, whole, 1 / ZOOM_STEP))}
      />
      <Button
        size="sm"
        variant="quiet"
        text="Earlier"
        press={() => change(panned(view, whole, -PAN_STEP))}
      />
      <Button
        size="sm"
        variant="quiet"
        text="Later"
        press={() => change(panned(view, whole, PAN_STEP))}
      />
      <Button size="sm" variant="quiet" text="Whole range" press={() => change(whole)} />
    </div>
  );
}

/** The words a row's accessible name adds to its label: how many rows it holds, and whether they show. */
function rowName(row: GanttRow): string {
  const count = row.count === undefined || row.count === 0 ? '' : ` ×${row.count}`;
  const fold = row.fold === undefined ? '' : row.fold.open ? ', open' : ', folded';
  return `${row.label}${count}${fold}`;
}

/** Right opens a closed fold and Left closes an open one, as in a tree. */
function foldKeys(event: KeyboardEvent<HTMLElement>, row: GanttRow): boolean {
  const {fold} = row;
  if (fold === undefined) return false;
  if ((event.key === 'ArrowRight' && !fold.open) || (event.key === 'ArrowLeft' && fold.open)) {
    event.preventDefault();
    fold.toggle();
    return true;
  }
  return false;
}

/** The name cell of a row: its lineage, the chevron of its fold, its label and what is folded into it. */
function RowName(props: {readonly row: GanttRow; readonly foldable: boolean}): JSX.Element {
  const {row} = props;
  const {fold} = row;
  return (
    <span className="vs-gantt-name" title={rowName(row)}>
      <Guides guides={row.guides} />
      {props.foldable ? (
        <span
          className="vs-tree-chevron vs-gantt-fold"
          data-open={fold?.open || undefined}
          data-empty={fold === undefined || undefined}
          aria-hidden="true"
          onClick={
            fold === undefined
              ? undefined
              : event => {
                  event.stopPropagation();
                  fold.toggle();
                }
          }
        >
          {fold === undefined ? null : <Icon name="chevron-right" />}
        </span>
      ) : null}
      <Text variant="code">{row.label}</Text>
      {row.count === undefined || row.count === 0 ? null : (
        <span className="vs-gantt-count">{`×${row.count}`}</span>
      )}
    </span>
  );
}

/** One row of a `Gantt`: an option of its listbox. */
function RowView(props: {
  readonly row: GanttRow;
  readonly index: number;
  readonly size: number;
  readonly stop: number;
  readonly strip: boolean;
  readonly foldable: boolean;
  readonly tagged: boolean;
  readonly entering: boolean;
  readonly view: GanttView;
  readonly aggregate: boolean;
  readonly playhead: StyleWithVars | null;
}): JSX.Element {
  const {row} = props;
  return (
    <div
      className="vs-gantt-row"
      role="option"
      aria-label={rowName(row)}
      aria-posinset={props.index + 1}
      aria-setsize={props.size}
      tabIndex={props.index === props.stop ? 0 : -1}
      aria-selected={row.selected === true}
      data-fresh={row.fresh === true || undefined}
      data-entering={props.entering || undefined}
      onClick={row.press}
      onKeyDown={event => {
        if (!foldKeys(event, row)) pickKeys(event, row.press);
      }}
    >
      {props.strip ? null : <RowName row={row} foldable={props.foldable} />}
      {props.tagged ? (
        <span className="vs-gantt-tag" title={row.tag}>
          {row.tag ?? ''}
        </span>
      ) : null}
      <span className="vs-gantt-track">
        <GanttBar row={row} view={props.view} aggregate={props.aggregate} />
        {props.playhead === null ? null : (
          <span className="vs-gantt-playhead" style={props.playhead} />
        )}
      </span>
      <span className="vs-gantt-figure" data-outcome={row.outcome} title={row.figureTitle}>
        {row.figure ?? ''}
      </span>
    </div>
  );
}

/** The words of a Gantt's key, one for each thing its bars can show; the caller's, in its own vocabulary. */
export interface GanttLegend {
  /** A bar that is still growing. */
  readonly live: string;
  readonly done: string;
  readonly failed: string;
  /** The line across a bar where a result came back. */
  readonly returned: string;
  /** The tag column's own word. */
  readonly tag: string;
  /** The stack drawn under a bar that holds several rows. */
  readonly folded: string;
}

/** The key to what a Gantt's bars say, drawn with the bars' own shades. */
function LegendKey(props: {readonly words: GanttLegend}): JSX.Element {
  const {words} = props;
  const item = (key: string, sample: JSX.Element, label: string): JSX.Element => (
    <li key={key} className="vs-gantt-legend-item">
      {sample}
      <Text variant="small" tone="muted">
        {label}
      </Text>
    </li>
  );
  const bar = (kind: string, outcome: string, stacked?: boolean): JSX.Element => (
    <span
      className="vs-gantt-legend-bar"
      data-outcome={outcome}
      data-stacked={stacked === true || undefined}
      aria-hidden="true"
    >
      <span className="vs-gantt-legend-fill" data-kind={kind} />
    </span>
  );
  return (
    <ul className="vs-gantt-legend" aria-label="Key to the bars">
      {item('live', bar('solid', 'live'), words.live)}
      {item('done', bar('grey', 'muted'), words.done)}
      {item('failed', bar('err', 'err'), words.failed)}
      {item(
        'returned',
        <span className="vs-gantt-legend-mark" aria-hidden="true" />,
        words.returned,
      )}
      {item(
        'tag',
        <span className="vs-gantt-tag vs-gantt-legend-tag" aria-hidden="true">
          {'\u00a0'}
        </span>,
        words.tag,
      )}
      {item('folded', bar('grey', 'muted', true), words.folded)}
    </ul>
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
 * Left and Right arrows do the same; bars clip at the edges of the view. Many rows read as plain
 * bars, without their pieces (`detail` `aggregate`, the default above 120 rows); zoom in to the
 * rows that matter, or pass `detail` `full`.
 *
 * Rows nest as a tree when they carry `guides` and `fold`s: every row's name, tag, bar and figure
 * keep their own columns at any depth, the lineage and chevron are the name column's indent. With
 * `tagLabel` the tags have a column of their own. A row that arrives while the Gantt is on screen
 * draws in with a short motion, and bars follow the axis smoothly as it grows.
 */
export function Gantt(props: {
  readonly label: string;
  readonly from: number;
  readonly to: number;
  readonly playhead?: number;
  /** The ticks of the axis: given, or `axis` for a time axis the Gantt ticks to its width. */
  readonly ticks?: readonly GanttTick[];
  readonly axis?: GanttAxis;
  readonly rows: readonly GanttRow[];
  readonly strip?: boolean;
  /** Column names on the tick row: over the names, the tags and the figures. */
  readonly nameLabel?: string;
  readonly tagLabel?: string;
  /** Keep a tag that carries primary row identity visible at narrow widths; secondary tags yield to the chart. */
  readonly tagPriority?: 'secondary' | 'primary';
  readonly figureLabel?: string;
  readonly view?: GanttView;
  readonly changeView?: (view: GanttView) => void;
  readonly detail?: 'full' | 'aggregate';
  /** `fill` takes the height of its container; the default is at most a thread's. */
  readonly size?: 'default' | 'fill';
  /** A key above the rows to what the bars, the lines on them, the tags and the stacks mean, in these words. */
  readonly legend?: GanttLegend;
}): JSX.Element {
  const whole: GanttView = {from: props.from, to: props.to};
  const view = props.view ?? whole;
  const strip = props.strip === true;
  const axis = axisOf(props);
  const {rows, changeView} = props;
  const detail = props.detail ?? (rows.length > AGGREGATE_ABOVE ? 'aggregate' : 'full');
  const foldable = rows.some(row => row.fold !== undefined);
  const tagged = props.tagLabel !== undefined;
  const entering = useEntering(rows.map(row => row.id));
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
  const stop = Math.max(
    0,
    rows.findIndex(row => row.selected === true),
  );
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
    <div
      className="vs-gantt"
      data-strip={strip || undefined}
      data-tags={tagged || undefined}
      data-tag-priority={props.tagPriority ?? 'secondary'}
      data-size={props.size ?? 'default'}
    >
      {props.legend === undefined ? null : <LegendKey words={props.legend} />}
      {strip || changeView === undefined ? null : (
        <ZoomBar view={view} whole={whole} change={changeView} />
      )}
      {strip || axis === undefined ? null : (
        <AxisRow
          ticks={axis}
          view={view}
          nameLabel={props.nameLabel}
          tagLabel={props.tagLabel}
          figureLabel={props.figureLabel}
        />
      )}
      <ScrollArea
        kind="feed"
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
          <RowView
            key={row.id}
            row={row}
            index={win.first + offset}
            size={rows.length}
            stop={stop}
            strip={strip}
            foldable={foldable}
            tagged={tagged}
            entering={entering.has(row.id)}
            view={view}
            aggregate={detail === 'aggregate'}
            playhead={playhead}
          />
        ))}
        <div aria-hidden="true" style={{height: win.after}} />
      </ScrollArea>
    </div>
  );
}
