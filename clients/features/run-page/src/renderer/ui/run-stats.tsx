/**
 * The stats row of the dashboard: the ideas the run tried, its measurements, the time its agents
 * worked within the time it ran, and its tokens. The figures are the state's (`runStats`); this
 * draws them under the time bar and above the charts.
 */
import type {TokenTally} from '@vibesys/run-state';
import {Row, Stat} from '@vibesys/ui';
import type {JSX} from 'react';
import {type ObjectiveSeries, type RunModel, runStats, statTiles} from '../../state/index.js';

export function RunStats(props: {
  /** The run at the position of the time bar. */
  readonly model: RunModel;
  /** The run to its end: it holds the sessions the running time is read from. */
  readonly whole: RunModel;
  /** The run's start on its clock. */
  readonly start: number;
  readonly tokens: TokenTally | null;
  readonly objectives: readonly ObjectiveSeries[];
}): JSX.Element {
  const tiles = statTiles(
    runStats(props.model, props.whole, props.start, props.tokens, props.objectives),
  );
  return (
    <Row gap="space-8" wrap align="start">
      {tiles.map(tile => (
        <Stat
          key={tile.id}
          label={tile.label}
          value={tile.figure}
          quiet={tile.quiet}
          {...(tile.unit === null ? {} : {unit: tile.unit})}
          {...(tile.note === '' ? {} : {sub: tile.note})}
        />
      ))}
    </Row>
  );
}
