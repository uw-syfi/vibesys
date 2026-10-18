import {type JSX, type ReactNode, useId} from 'react';

/** One tile: the value it stands for, its name, and what leads the name (a mark, an icon). */
export interface TileOption<T extends string> {
  readonly value: T;
  readonly label: string;
  readonly leading?: ReactNode;
}

/**
 * A radio group drawn as a row of tiles, each a `leading` mark above or beside its name; built on
 * native radio inputs like `SegmentedControl`: one tab stop (the chosen tile), and the arrow keys
 * move the focus and choose, wrapping at the ends. For a few choices that are products or
 * things with a picture; plain words belong in a `SegmentedControl`.
 */
export function ChoiceTiles<T extends string>(props: {
  /** The group's accessible name. */
  readonly label: string;
  readonly options: readonly TileOption<T>[];
  readonly value: T;
  readonly change: (value: T) => void;
}): JSX.Element {
  const name = useId();
  return (
    <div className="vs-choice-tiles" role="radiogroup" aria-label={props.label}>
      {props.options.map(option => (
        <label key={option.value} className="vs-choice-tile">
          <input
            className="vs-choice-tile-input"
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === props.value}
            onChange={() => props.change(option.value)}
          />
          {option.leading === undefined ? null : (
            <span className="vs-choice-tile-leading">{option.leading}</span>
          )}
          <span className="vs-choice-tile-label">{option.label}</span>
        </label>
      ))}
    </div>
  );
}
