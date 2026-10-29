import {type JSX, useId} from 'react';

/** One choice: the value it stands for and the text it shows. */
export interface ChoiceOption<T extends string> {
  readonly value: T;
  readonly label: string;
}

/**
 * A radio group drawn as segments, built on native radio inputs: one tab stop (the chosen segment),
 * and the arrow keys move the focus and choose, wrapping at the ends.
 */
export function SegmentedControl<T extends string>(props: {
  /** The group's accessible name. */
  readonly label: string;
  readonly options: readonly ChoiceOption<T>[];
  readonly value: T;
  readonly change: (value: T) => void;
  /** `xs` is `sm` with tighter gaps and padding, for a switch in a narrow pane. */
  readonly size?: 'xs' | 'sm' | 'md';
}): JSX.Element {
  const name = useId();
  return (
    <div
      className="vs-segmented"
      role="radiogroup"
      aria-label={props.label}
      data-size={props.size ?? 'md'}
    >
      {props.options.map(option => (
        <label key={option.value} className="vs-segment" data-label={option.label}>
          <input
            className="vs-segment-input"
            type="radio"
            name={name}
            value={option.value}
            checked={option.value === props.value}
            onChange={() => props.change(option.value)}
          />
          {option.label}
        </label>
      ))}
    </div>
  );
}
