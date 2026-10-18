import type {JSX} from 'react';
import {useFieldWiring} from './field.js';
import {Icon} from './icon.js';

/** One option; with `disabledReason` it cannot be chosen and shows why after its label. */
export interface SelectOption<T extends string> {
  readonly value: T;
  readonly label: string;
  readonly disabledReason?: string;
}

function optionText<T extends string>(option: SelectOption<T>): string {
  return option.disabledReason === undefined
    ? option.label
    : `${option.label} (${option.disabledReason})`;
}

/** A native select inside a `Field`, drawn by the tokens; `mono` sets the option text in mono. */
export function Select<T extends string>(props: {
  readonly value: T;
  readonly options: readonly SelectOption<T>[];
  readonly change: (value: T) => void;
  readonly mono?: boolean;
  /** `sm` is the height of the other small controls (a search box), for a toolbar row. */
  readonly size?: 'sm';
}): JSX.Element {
  const wiring = useFieldWiring('Select');
  const {options, change} = props;
  return (
    <span className="vs-select" data-size={props.size}>
      <select
        className="vs-input"
        id={wiring.id}
        value={props.value}
        data-mono={props.mono === true || undefined}
        data-mono-id={props.mono === true || undefined}
        aria-describedby={wiring.describedBy}
        aria-invalid={wiring.invalid || undefined}
        onChange={event => {
          const chosen = options.find(option => option.value === event.target.value);
          if (chosen !== undefined) change(chosen.value);
        }}
      >
        {options.map(option => (
          <option
            key={option.value}
            value={option.value}
            disabled={option.disabledReason !== undefined}
          >
            {optionText(option)}
          </option>
        ))}
      </select>
      <span className="vs-select-chevron">
        <Icon name="chevron-down" />
      </span>
    </span>
  );
}
