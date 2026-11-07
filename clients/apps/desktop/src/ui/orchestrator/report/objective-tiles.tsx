import {BoundUnitCheck} from '@vibesys/orchestrator-proto';
import {Meter, MetricTile, MetricTiles} from '@vibesys/ui';
import type {JSX} from 'react';
import type {ObjectiveReport} from '../../../state/index.js';
import {formatExact, formatFigure, withUnit} from './format.js';

/** The words a tile's hover says: what the head scored, the best and the bound. Head and best differ when the head is the best of another objective. */
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

/**
 * One tile for each objective: its best measured value, the number the header and the over-time chart
 * show, and how close that is to the bound the agents declared, as the host worked it out. The head's
 * own value is in the hover. An objective with no value shows a dash and one with no bound shows no
 * share.
 */
export function ObjectiveTiles(props: {
  readonly objectives: readonly ObjectiveReport[];
}): JSX.Element | null {
  if (props.objectives.length === 0) return null;
  return (
    <MetricTiles label="Objectives" count={props.objectives.length} track="chart">
      {props.objectives.map(objective => {
        const value = objective.bestValue;
        const percent = objective.bestPercentOfBound;
        const hint = hintOf(objective);
        const mismatch = objective.boundUnitCheck === BoundUnitCheck.MISMATCH;
        const detail = mismatch
          ? `${objective.unit ?? '?'} vs bound ${objective.boundUnit ?? '?'}`
          : percent === undefined
            ? ' '
            : `${Math.round(percent)}% of bound${objective.boundUnitCheck === BoundUnitCheck.UNKNOWN ? ' · unverified' : ''}`;
        return (
          <MetricTile
            key={objective.name}
            name={objective.name}
            figure={value === undefined ? '—' : formatFigure(value)}
            {...(objective.unit === null ? {} : {note: objective.unit})}
            tone={value === undefined ? 'muted' : 'ok'}
            detail={detail}
            {...(hint === undefined ? {} : {hint})}
            {...(percent === undefined
              ? {}
              : {
                  trend: (
                    <Meter
                      decorative
                      label={`${objective.name} against its bound`}
                      value={percent / 100}
                    />
                  ),
                })}
          />
        );
      })}
    </MetricTiles>
  );
}
