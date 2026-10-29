import {type JSX, type ReactNode, useId} from 'react';

/**
 * One tile: the value it stands for, its name, and what leads the name (a mark, an icon). A tile
 * may say more under its name: a `note` (what it is) and a `meta` line (what it has). With
 * `unavailable` it cannot be chosen and says why in place of `meta`.
 */
export interface TileOption<T extends string> {
  readonly value: T;
  readonly label: string;
  readonly leading?: ReactNode;
  readonly note?: string;
  readonly meta?: string;
  readonly unavailable?: string;
  /** Set the name in the code face, for an identifier. */
  readonly mono?: boolean;
}

/** `row` is one line of compact tiles; `cards` are roomier tiles in a grid that wraps. */
export type TileLayout = 'row' | 'cards';

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
  /** The chosen value; `null` while nothing is chosen. */
  readonly value: T | null;
  readonly change: (value: T) => void;
  readonly layout?: TileLayout;
}): JSX.Element {
  const name = useId();
  return (
    <div
      className="vs-choice-tiles"
      role="radiogroup"
      aria-label={props.label}
      data-layout={props.layout ?? 'row'}
    >
      {props.options.map(option => (
        <label
          key={option.value}
          className="vs-choice-tile"
          data-unavailable={option.unavailable === undefined ? undefined : ''}
        >
          <input
            className="vs-choice-tile-input"
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === props.value}
            disabled={option.unavailable !== undefined}
            onChange={() => props.change(option.value)}
          />
          {option.leading === undefined ? null : (
            <span className="vs-choice-tile-leading">{option.leading}</span>
          )}
          <span className="vs-choice-tile-text">
            <span
              className="vs-choice-tile-label"
              data-mono={option.mono === true || undefined}
              data-mono-id={option.mono === true || undefined}
            >
              {option.label}
            </span>
            {option.note === undefined ? null : (
              <span className="vs-choice-tile-note">{option.note}</span>
            )}
            {option.unavailable === undefined && option.meta === undefined ? null : (
              <span className="vs-choice-tile-meta">{option.unavailable ?? option.meta}</span>
            )}
          </span>
        </label>
      ))}
    </div>
  );
}
