import {type JSX, useId} from 'react';
import {Button} from './button.js';
import type {StyleWithVars} from './css-vars.js';
import {placeLabels, useInlineSize} from './label-fit.js';
import {Chip} from './status.js';
import {Text} from './text.js';

/** A labelled point of the sequence, drawn under the track. */
export interface ScrubberTick {
  readonly at: number;
  readonly label: string;
}

/**
 * A range over the positions 0 to `last` of a sequence, with labelled ticks and the playhead at
 * `value`. It reads `latestLabel` ("Live" unless given) at the newest position (`live`), and
 * `pastLabel` anywhere else, where it offers `backLabel` ("Back to live" unless given); a sequence
 * that has ended reads, for example, "End" and "Jump to end". The arrows, Page Up and Down, Home
 * and End step it (a native range).
 * `position` describes the playhead in words (for example a time), read out as its value.
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
  readonly change: (value: number) => void;
  readonly backToLive: () => void;
}): JSX.Element {
  const id = useId();
  const share = (at: number): number => (props.last === 0 ? 0 : at / props.last);
  const [measure, width] = useInlineSize();
  const ticks = props.ticks ?? [];
  // Once the track is measured only the labels that can be read side by side are drawn.
  const fitted =
    width === null
      ? null
      : placeLabels(
          ticks.map(tick => ({share: share(tick.at), text: tick.label})),
          width,
        );
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
        max={props.last}
        step={1}
        value={props.value}
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
                {tick.label}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
