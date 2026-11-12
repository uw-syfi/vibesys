import {BoundUnitCheck} from '@vibesys/orchestrator-proto';
import {Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ObjectiveReport} from '../../../state/index.js';
import {formatExact, formatFigure, withUnit} from './format.js';
import {yet} from './yet.js';

/** The words a figure's hover says: what the head scored, the best and the bound. Head and best differ when the head is the best of another objective. */
function hintOf(objective: ObjectiveReport): string | undefined {
  const lines = [
    objective.headValue === undefined
      ? undefined
      : `Head ${withUnit(formatExact(objective.headValue), objective.unit)}`,
    objective.bestValue === undefined
      ? undefined
      : `Best ${withUnit(formatExact(objective.bestValue), objective.unit)}`,
    objective.bound === undefined
      ? undefined
      : `Bound ${withUnit(formatExact(objective.bound.current.value), objective.bound.current.unit)}`,
  ].filter((line): line is string => line !== undefined);
  return lines.length === 0 ? undefined : lines.join('\n');
}

/** What the block says under its figure: the share of the bound, or both units when they differ; null when there is no bound. */
function boundWords(objective: ObjectiveReport): string | null {
  if (objective.boundUnitCheck === BoundUnitCheck.MISMATCH) {
    return `${objective.unit ?? '?'} vs bound ${objective.boundUnit ?? '?'}`;
  }
  const percent = objective.bestPercentOfBound;
  if (percent === undefined) return null;
  const unverified = objective.boundUnitCheck === BoundUnitCheck.UNKNOWN ? ' · unverified' : '';
  const bound = objective.bound?.current.value;
  return `${Math.round(percent)}% of bound${bound === undefined ? '' : ` ${formatFigure(bound)}`}${unverified}`;
}

/**
 * What a band says of one objective beside its chart: its name and which way is better, its best
 * value with the unit, and how close that is to the bound the agents declared, as the host worked it
 * out. With nothing measured the best is a dash and the block says so; with a window chosen it says
 * how many of the measurements are in view. What the objective is against the input is not here: the
 * host serves no run-level figure for it.
 */
export function ObjectiveSummary(props: {
  readonly objective: ObjectiveReport;
  /** How many measurements the chart shows, when it shows a window and not the whole run. */
  readonly inView: {readonly shown: number; readonly of: number} | null;
  /** The run has ended: a value not measured by now never will be. */
  readonly runOver: boolean;
}): JSX.Element {
  const {objective, inView} = props;
  const best = objective.bestValue;
  const bound = boundWords(objective);
  const hint = hintOf(objective);
  return (
    <Stack gap="space-1">
      <Text variant="item" title={objective.name}>
        {objective.name}
      </Text>
      <Text variant="small" tone="muted">
        {objective.better === null
          ? 'Direction unavailable'
          : objective.better === 'lower'
            ? 'Lower is better'
            : 'Higher is better'}
      </Text>
      <Row gap="space-2" align="baseline" wrap>
        <Text
          variant="metric"
          tone={best === undefined ? 'muted' : 'default'}
          {...(hint === undefined ? {} : {title: hint})}
        >
          {best === undefined ? '—' : formatFigure(best)}
        </Text>
        {objective.unit === null || best === undefined ? null : (
          <Text variant="small" tone="muted">
            {objective.unit}
          </Text>
        )}
      </Row>
      {best === undefined ? (
        <Text variant="small" tone="muted">
          {yet('Not measured', props.runOver)}
        </Text>
      ) : null}
      {bound === null ? null : (
        <Text variant="small" tone="muted">
          {bound}
        </Text>
      )}
      {inView === null ? null : (
        <Text variant="small" tone="muted">
          {`${formatExact(inView.shown)} of ${formatExact(inView.of)} in view`}
        </Text>
      )}
    </Stack>
  );
}
