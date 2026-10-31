import {type JSX, useId} from 'react';
import {Button} from './button.js';
import type {StyleWithVars} from './css-vars.js';
import {type PlacedLabel, placeLabels, useInlineSize} from './label-fit.js';
import {Chip} from './status.js';
import {Text} from './text.js';

/** A labelled point of the sequence, drawn under the track. */
export interface ScrubberTick {
  readonly at: number;
  readonly label: string;
}

/**
 * The labels of `ticks` that fit a track `width` px wide, with the word before each if all of them
 * fit so, else without it; none placed (`fitted` null) while the track is unmeasured.
 */
function fitTicks(
  ticks: readonly ScrubberTick[],
  share: (at: number) => number,
  word: string | undefined,
  width: number | null,
): {readonly fitted: readonly PlacedLabel[] | null; readonly words: string | undefined} {
  if (width === null) return {fitted: null, words: undefined};
  const along = (prefix: string | undefined) =>
    ticks.map(tick => ({
      share: share(tick.at),
      text: prefix === undefined ? tick.label : `${prefix} ${tick.label}`,
    }));
  const worded = word === undefined ? null : placeLabels(along(word), width);
  return worded !== null && worded.length === ticks.length
    ? {fitted: worded, words: word}
    : {fitted: placeLabels(along(undefined), width), words: undefined};
}

/** The resolution of a track spaced by `places`: the range reads 0 to this. */
const SPAN = 1000;

/** The newest position at or before `along` (0 to 1) of a track spaced by `places`. */
function positionAt(places: readonly number[], along: number): number {
  let found = 0;
  for (let at = 0; at < places.length; at += 1) {
    if ((places[at] ?? 0) <= along) found = at;
  }
  return found;
}

/** Where a key moves the playhead from position `value` of `last`, or null for a key that does not move it. */
function keyed(key: string, value: number, last: number): number | null {
  const jump = Math.max(1, Math.trunc(last / 10));
  const moved: Record<string, number> = {
    ArrowLeft: value - 1,
    ArrowDown: value - 1,
    ArrowRight: value + 1,
    ArrowUp: value + 1,
    PageDown: value - jump,
    PageUp: value + jump,
    Home: 0,
    End: last,
  };
  const target = moved[key];
  return target === undefined ? null : Math.max(0, Math.min(last, target));
}

/**
 * The range of a `Scrubber`: over the positions themselves, or, when the track is spaced by `places`,
 * over `SPAN` steps of it, a point on it standing for the newest position at or before it.
 */
function Track(props: {
  readonly id: string;
  readonly label: string;
  readonly position: string;
  readonly value: number;
  readonly last: number;
  readonly single: boolean;
  readonly places: readonly number[] | undefined;
  readonly change: (value: number) => void;
}): JSX.Element {
  const {places, single} = props;
  const along = places === undefined ? 0 : (places[props.value] ?? 0);
  return (
    <input
      id={props.id}
      className="vs-scrubber-range"
      type="range"
      min={0}
      max={single ? 1 : places === undefined ? props.last : SPAN}
      step={1}
      value={single ? 1 : places === undefined ? props.value : Number((along * SPAN).toFixed(0))}
      disabled={single}
      aria-label={props.label}
      aria-valuetext={props.position}
      onChange={event =>
        props.change(
          places === undefined
            ? Number(event.target.value)
            : positionAt(places, Number(event.target.value) / SPAN),
        )
      }
      onKeyDown={event => {
        const next = places === undefined ? null : keyed(event.key, props.value, props.last);
        if (next === null) return;
        event.preventDefault();
        props.change(next);
      }}
    />
  );
}

/** A small diamond on the track, `at` (0 to 1) of the way along it; it takes no pointer or focus. */
function Mark(props: {readonly at: number}): JSX.Element {
  const place: StyleWithVars = {'--mark-at': props.at};
  return <span className="vs-scrubber-mark" aria-hidden="true" style={place} />;
}

/**
 * A range over the positions 0 to `last` of a sequence, with labelled ticks and the playhead at
 * `value`. It reads `latestLabel` ("Live" unless given) at the newest position (`live`), and
 * `pastLabel` anywhere else, where it offers `backLabel` ("Back to live" unless given); a sequence
 * that has ended reads, for example, "End" and "Jump to end". The arrows, Page Up and Down, Home
 * and End step it (a native range).
 * `position` describes the playhead in words (for example a time), read out as its value.
 * With `places` (where each position stands along the track, 0 to 1, ascending) the track is spaced
 * by them, not evenly: a long quiet stretch is a long stretch of track, as on a chart over the same
 * time. The arrows, Page Up and Down, Home and End then step by position, as the native range does.
 * A sequence with one position has nothing to scrub: the track is drawn whole with the playhead at
 * its end (the one position is the newest) and cannot be moved, instead of a zero-width range with
 * the playhead at the start of a track that reads "End".
 */
export function Scrubber(props: {
  readonly label: string;
  readonly value: number;
  readonly last: number;
  readonly position: string;
  readonly live: boolean;
  /** What the state reads away from the newest position. */
  readonly pastLabel: string;
  /** What the state reads at the newest position; "Live" unless given. */
  readonly latestLabel?: string;
  /** The action that returns to the newest position; "Back to live" unless given. */
  readonly backLabel?: string;
  readonly ticks?: readonly ScrubberTick[];
  /** Where each position (0 to `last`) stands along the track, 0 to 1; even when absent. */
  readonly places?: readonly number[];
  /** Where a small diamond is drawn on the track, each 0 to 1 along it: the moments worth finding. */
  readonly marks?: readonly number[];
  /**
   * What a tick's label counts (`Call`): each label reads `Call 3` while every one fits beside the
   * others, and the bare number once they would not, so labels never skip.
   */
  readonly tickWord?: string;
  readonly change: (value: number) => void;
  readonly backToLive: () => void;
}): JSX.Element {
  const id = useId();
  const single = props.last === 0;
  const places =
    props.places !== undefined && props.places.length === props.last + 1 && !single
      ? props.places
      : undefined;
  const share = (at: number): number =>
    places !== undefined ? (places[at] ?? 0) : props.last === 0 ? 0 : at / props.last;
  const [measure, width] = useInlineSize();
  const ticks = props.ticks ?? [];
  // Once the track is measured only the labels that can be read side by side are drawn.
  const {fitted, words} = fitTicks(ticks, share, props.tickWord, width);
  return (
    <div className="vs-scrubber">
      <div className="vs-scrubber-head">
        <label className="vs-scrubber-label" htmlFor={id}>
          <Text variant="code">{props.position}</Text>
        </label>
        <Chip
          tone={props.live ? 'live' : 'neutral'}
          dot
          label={props.live ? (props.latestLabel ?? 'Live') : props.pastLabel}
        />
        {props.live ? null : (
          <Button size="sm" text={props.backLabel ?? 'Back to live'} press={props.backToLive} />
        )}
      </div>
      <div className="vs-scrubber-track">
        <Track
          id={id}
          label={props.label}
          position={props.position}
          value={props.value}
          last={props.last}
          single={single}
          places={places}
          change={props.change}
        />
        {single ? null : (props.marks ?? []).map(at => <Mark key={at} at={at} />)}
      </div>
      {props.ticks === undefined ? null : (
        <div ref={measure} className="vs-scrubber-ticks" aria-hidden="true">
          {(fitted ?? ticks.map((_, index) => ({index, left: null}))).map(({index, left}) => {
            const tick = ticks[index];
            if (tick === undefined) return null;
            const place: StyleWithVars =
              left === null
                ? {'--tick-at': `${share(tick.at) * 100}%`}
                : {'--tick-left': `${left}px`};
            return (
              <span
                key={tick.at}
                className="vs-scrubber-tick"
                data-placed={left === null ? undefined : ''}
                style={place}
              >
                {words === undefined ? tick.label : `${words} ${tick.label}`}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
