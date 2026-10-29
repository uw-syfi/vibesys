import type {JSX} from 'react';
import {useFieldWiring} from './field.js';
import {Icon} from './icon.js';

/**
 * One option; with `disabledReason` it cannot be chosen and shows why after its label. Options with
 * the same `group` are listed under that heading (list the options of a group together).
 */
export interface SelectOption<T extends string> {
  readonly value: T;
  readonly label: string;
  readonly disabledReason?: string;
  readonly group?: string;
}

/** `options` split into segments of one group, in order; an option with no group is a segment of its own. */
function segments<T extends string>(
  options: readonly SelectOption<T>[],
): {readonly group: string | undefined; readonly options: SelectOption<T>[]}[] {
  const found: {group: string | undefined; options: SelectOption<T>[]}[] = [];
  for (const option of options) {
    const last = found.at(-1);
    if (last !== undefined && last.group === option.group && option.group !== undefined) {
      last.options.push(option);
    } else {
      found.push({group: option.group, options: [option]});
    }
  }
  return found;
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
        {segments(options).map(segment => {
          const items = segment.options.map(option => (
            <option
              key={option.value}
              value={option.value}
              disabled={option.disabledReason !== undefined}
            >
              {optionText(option)}
            </option>
          ));
          return segment.group === undefined ? (
            items
          ) : (
            <optgroup key={segment.group} label={segment.group}>
              {items}
            </optgroup>
          );
        })}
      </select>
      <span className="vs-select-chevron">
        <Icon name="chevron-down" />
      </span>
    </span>
  );
}
