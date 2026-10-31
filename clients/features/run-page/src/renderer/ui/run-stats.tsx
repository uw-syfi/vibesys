/**
 * The stats row of the dashboard: the ideas the run tried, its measurements, the time its agents
 * worked within the time it ran, and its tokens. The figures are the state's (`runStats`); this
 * draws them under the time bar and above the charts.
 */
import type {TokenLedger} from '@vibesys/run-state';
import {Row, Stat} from '@vibesys/ui';
import type {JSX} from 'react';
import {type RunModel, type RunPosition, runStats, statTiles} from '../../state/index.js';

export function RunStats(props: {
  /** The run at the position of the time bar. */
  readonly model: RunModel;
  /** The run to its end and the time bar's place on it. */
  readonly position: RunPosition;
  /** The run's start on its clock. */
  readonly start: number;
  /** What the run's turns spent, to its end. */
  readonly ledger: TokenLedger;
}): JSX.Element {
  const tiles = statTiles(runStats(props.model, props.position, props.start, props.ledger));
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
