/** The two limits of a run, as Review's rows: how many ideas it tries and how long it may take. */

import {Field, Select} from '@vibesys/ui';
import type {JSX} from 'react';
import type {Budget} from '../../state/index.js';
import type {CardProps} from './card-props.js';

const ROUNDS = ['configured', '1', '2', '3', '5', '10'] as const;
const TIME_LIMITS = [
  {value: 'none', label: 'None', seconds: null},
  {value: '3600', label: '1 hour', seconds: 3600},
  {value: '28800', label: '8 hours', seconds: 28800},
  {value: '86400', label: '24 hours', seconds: 86400},
] as const;

/** `options`, plus the form's own `value` when none of them is it (a run started with another one). */
function withCurrent(
  options: readonly {readonly value: string; readonly label: string}[],
  value: string,
  label: string,
): {readonly value: string; readonly label: string}[] {
  return options.some(option => option.value === value)
    ? [...options]
    : [...options, {value, label}];
}

function change(props: CardProps, next: Partial<Budget>): void {
  props.dispatch({type: 'budget', budget: {...props.context.form.budget, ...next}});
}

/** How many ideas the run tries before it stops. */
export function IdeasField(props: CardProps): JSX.Element {
  const {rounds} = props.context.form.budget;
  return (
    <Field
      row
      label="Ideas to try"
      help={rounds === null ? 'Task default' : `${rounds} ${rounds === 1 ? 'idea' : 'ideas'}`}
    >
      <Select
        value={rounds === null ? 'configured' : String(rounds)}
        options={withCurrent(
          ROUNDS.map(value => ({
            value,
            label: value === 'configured' ? 'As the task is configured' : value,
          })),
          rounds === null ? 'configured' : String(rounds),
          String(rounds),
        )}
        change={value => change(props, {rounds: value === 'configured' ? null : Number(value)})}
      />
    </Field>
  );
}

/** How long the run may take. */
export function TimeLimitField(props: CardProps): JSX.Element {
  const {timeLimitS} = props.context.form.budget;
  return (
    <Field row label="Time limit">
      <Select
        value={timeLimitS === null ? 'none' : String(timeLimitS)}
        options={withCurrent(
          TIME_LIMITS.map(({value, label}) => ({value, label})),
          timeLimitS === null ? 'none' : String(timeLimitS),
          `${timeLimitS} seconds`,
        )}
        change={value => change(props, {timeLimitS: value === 'none' ? null : Number(value)})}
      />
    </Field>
  );
}
