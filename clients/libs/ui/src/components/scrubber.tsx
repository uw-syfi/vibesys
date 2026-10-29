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
  last: number,
  word: string | undefined,
  width: number | null,
): {readonly fitted: readonly PlacedLabel[] | null; readonly words: string | undefined} {
  if (width === null) return {fitted: null, words: undefined};
  const along = (prefix: string | undefined) =>
    ticks.map(tick => ({
      share: last === 0 ? 0 : tick.at / last,
      text: prefix === undefined ? tick.label : `${prefix} ${tick.label}`,
    }));
  const worded = word === undefined ? null : placeLabels(along(word), width);
  return worded !== null && worded.length === ticks.length
    ? {fitted: worded, words: word}
    : {fitted: placeLabels(along(undefined), width), words: undefined};
}

/**
 * A range over the positions 0 to `last` of a sequence, with labelled ticks and the playhead at
 * `value`. It reads `latestLabel` ("Live" unless given) at the newest position (`live`), and
 * `pastLabel` anywhere else, where it offers `backLabel` ("Back to live" unless given); a sequence
 * that has ended reads, for example, "End" and "Jump to end". The arrows, Page Up and Down, Home
 * and End step it (a native range).
 * `position` describes the playhead in words (for example a time), read out as its value.
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
  const share = (at: number): number => (props.last === 0 ? 0 : at / props.last);
  const [measure, width] = useInlineSize();
  const ticks = props.ticks ?? [];
  // Once the track is measured only the labels that can be read side by side are drawn.
  const {fitted, words} = fitTicks(ticks, props.last, props.tickWord, width);
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
      <input
        id={id}
        className="vs-scrubber-range"
        type="range"
        min={0}
        max={single ? 1 : props.last}
        step={1}
        value={single ? 1 : props.value}
        disabled={single}
        aria-label={props.label}
        aria-valuetext={props.position}
        onChange={event => props.change(Number(event.target.value))}
      />
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
