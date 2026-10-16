import type {JSX} from 'react';
import type {StyleWithVars} from './css-vars.js';
import {type Guide, Guides} from './guides.js';
import {optionKeys} from './listbox.js';
import {Text} from './text.js';

/** One piece of a Gantt bar: its span on the axis and its `kind`, a shade (solid accent, hatched, or grey), not words. */
export interface GanttSegment {
  readonly id: string;
  readonly start: number;
  readonly end: number;
  readonly kind: 'solid' | 'hatched' | 'grey';
  /** The one-line tooltip. */
  readonly label: string;
  /** Opens this piece. Pointer only: the row's own key opens the row, whose steps are a list. */
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

function percent(share: number): string {
  return `${(share * 100).toFixed(2)}%`;
}

/** Where `value` falls on the axis from `from` to `to`, as a share of it, kept within it. */
function shareOf(value: number, from: number, to: number): number {
  if (to <= from) return 0;
  return Math.max(0, Math.min(1, (value - from) / (to - from)));
}

function GanttBar(props: {
  readonly row: GanttRow;
  readonly from: number;
  readonly to: number;
}): JSX.Element {
  const {row, from, to} = props;
  const start = shareOf(row.start, from, to);
  const end = shareOf(row.end, from, to);
  const place: StyleWithVars = {
    '--gantt-from': percent(start),
    '--gantt-span': percent(Math.max(0, end - start)),
  };
  return (
    <span className="vs-gantt-bar" data-outcome={row.outcome} style={place}>
      {row.segments.map(segment => {
        const within = (value: number): number => shareOf(value, row.start, row.end);
        const seg: StyleWithVars = {
          '--seg-from': percent(within(segment.start)),
          '--seg-span': percent(Math.max(0, within(segment.end) - within(segment.start))),
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
  readonly from: number;
  readonly to: number;
  readonly nameLabel: string | undefined;
  readonly figureLabel: string | undefined;
}): JSX.Element {
  return (
    <div className="vs-gantt-row" data-axis="" aria-hidden="true">
      <span className="vs-gantt-name vs-listheader">{props.nameLabel ?? ''}</span>
      <span className="vs-gantt-track">
        {props.ticks.map(tick => {
          const place: StyleWithVars = {
            '--gantt-from': percent(shareOf(tick.at, props.from, props.to)),
          };
          return (
            <span key={tick.at} className="vs-gantt-tick" style={place}>
              {tick.label}
            </span>
          );
        })}
      </span>
      <span className="vs-gantt-figure vs-listheader">{props.figureLabel ?? ''}</span>
    </div>
  );
}

/**
 * Rows of bars on one time axis from `from` to `to`, a tick row above them, and a `playhead` line
 * through every row. Rows are the options of a listbox (Up and Down, Home and End move, Enter and
 * Space press; one tab stop, the selected row or the first); pressing a segment opens that
 * segment. `strip` drops the names and ticks: one thin row under a heading that already names it.
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
}): JSX.Element {
  const {from, to} = props;
  const strip = props.strip === true;
  const playhead: StyleWithVars | null =
    props.playhead === undefined
      ? null
      : {'--gantt-from': percent(shareOf(props.playhead, from, to))};
  const stop = props.rows.some(row => row.selected === true)
    ? props.rows.find(row => row.selected === true)?.id
    : props.rows[0]?.id;
  return (
    <div
      className="vs-gantt"
      role="listbox"
      aria-label={props.label}
      data-strip={strip || undefined}
    >
      {strip || props.ticks === undefined ? null : (
        <AxisRow
          ticks={props.ticks}
          from={from}
          to={to}
          nameLabel={props.nameLabel}
          figureLabel={props.figureLabel}
        />
      )}
      {props.rows.map(row => (
        <div
          key={row.id}
          className="vs-gantt-row"
          role="option"
          aria-label={row.label}
          tabIndex={row.id === stop ? 0 : -1}
          aria-selected={row.selected === true}
          data-fresh={row.fresh === true || undefined}
          onClick={row.press}
          onKeyDown={event => {
            optionKeys(event, row.press);
          }}
        >
          {strip ? null : (
            <span className="vs-gantt-name">
              <Guides guides={row.guides} />
              <Text variant="code">{row.label}</Text>
            </span>
          )}
          <span className="vs-gantt-track">
            <GanttBar row={row} from={from} to={to} />
            {playhead === null ? null : <span className="vs-gantt-playhead" style={playhead} />}
          </span>
          <span className="vs-gantt-figure" data-outcome={row.outcome}>
            {row.figure ?? ''}
          </span>
        </div>
      ))}
    </div>
  );
}
