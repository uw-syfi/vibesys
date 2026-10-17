/** The Budget card: how many rounds, and how long, at most. */

import {Card, Field, Grid, Select} from '@vibesys/ui';
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

export function BudgetCard(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const {budget} = context.form;
  const change = (next: Partial<Budget>): void =>
    dispatch({type: 'budget', budget: {...budget, ...next}});
  return (
    <Card section="Budget">
      <Grid min="field" gap="space-3">
        <Field label="Rounds" help="Searches without rounds, such as evolve, take none.">
          <Select
            value={budget.rounds === null ? 'configured' : String(budget.rounds)}
            options={withCurrent(
              ROUNDS.map(value => ({
                value,
                label: value === 'configured' ? 'As configured' : value,
              })),
              budget.rounds === null ? 'configured' : String(budget.rounds),
              String(budget.rounds),
            )}
            change={value => change({rounds: value === 'configured' ? null : Number(value)})}
          />
        </Field>
        <Field label="Time limit">
          <Select
            value={budget.timeLimitS === null ? 'none' : String(budget.timeLimitS)}
            options={withCurrent(
              TIME_LIMITS.map(({value, label}) => ({value, label})),
              budget.timeLimitS === null ? 'none' : String(budget.timeLimitS),
              `${budget.timeLimitS} seconds`,
            )}
            change={value => change({timeLimitS: value === 'none' ? null : Number(value)})}
          />
        </Field>
      </Grid>
    </Card>
  );
}
