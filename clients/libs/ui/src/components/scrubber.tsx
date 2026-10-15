import {type JSX, useId} from 'react';
import {Button} from './button.js';
import type {StyleWithVars} from './css-vars.js';
import {Chip} from './status.js';
import {Text} from './text.js';

/** A labelled point of the sequence, drawn under the track. */
export interface ScrubberTick {
  readonly at: number;
  readonly label: string;
}

/**
 * A range over the positions 0 to `last` of a sequence, with labelled ticks and the playhead at
 * `value`. It reads "Live" at the newest position (`live`), and `pastLabel` anywhere else, where
 * it offers "Back to live". The arrows, Page Up and Down, Home and End step it (a native range).
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
  readonly ticks?: readonly ScrubberTick[];
  readonly change: (value: number) => void;
  readonly backToLive: () => void;
}): JSX.Element {
  const id = useId();
  const share = (at: number): number => (props.last === 0 ? 0 : (at / props.last) * 100);
  return (
    <div className="vs-scrubber">
      <div className="vs-scrubber-head">
        <label className="vs-scrubber-label" htmlFor={id}>
          <Text variant="code">{props.position}</Text>
        </label>
        <Chip
          tone={props.live ? 'live' : 'neutral'}
          dot
          label={props.live ? 'Live' : props.pastLabel}
        />
        {props.live ? null : <Button size="sm" text="Back to live" press={props.backToLive} />}
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
        <div className="vs-scrubber-ticks" aria-hidden="true">
          {props.ticks.map(tick => {
            const place: StyleWithVars = {'--tick-at': `${share(tick.at)}%`};
            return (
              <span key={tick.at} className="vs-scrubber-tick" style={place}>
                {tick.label}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}
