/**
 * A machine's live stats on its card: gauges for CPU, memory and the GPUs, dimmed with the age of
 * the value when the host stopped delivering, or one status word and a Retry when there is nothing
 * to show. Labels, numbers and status words only.
 */
import {Button, Gauge, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {ageWord, type StatsView} from '../../state/index.js';
import {type LiveStats, useNow} from '../effects/live.js';

function Age(props: {readonly live: LiveStats; readonly valueAt: number}): JSX.Element {
  const now = useNow(props.live);
  return (
    <Text variant="small" tone="muted">
      {`Stale ${ageWord(now, props.valueAt)}`}
    </Text>
  );
}

export function MachineStats(props: {
  readonly view: StatsView;
  readonly live: LiveStats;
  readonly retry: () => void;
}): JSX.Element {
  const {view} = props;
  switch (view.kind) {
    case 'waiting':
      return <Text tone="muted">{view.word}</Text>;
    case 'failed':
      return (
        <Row gap="space-2" wrap align="center">
          <Text tone="muted">{view.word}</Text>
          {view.retry ? (
            <Button variant="quiet" size="sm" text="Retry" press={props.retry} />
          ) : null}
        </Row>
      );
    case 'values':
      return (
        <Row gap="space-5" wrap align="center">
          {view.gauges.map(gauge => (
            <Gauge
              key={gauge.label}
              label={gauge.label}
              share={gauge.share}
              figure={gauge.figure}
              tone={gauge.tone}
              dim={view.stale !== null}
            />
          ))}
          {view.stale === null ? null : <Age live={props.live} valueAt={view.stale.valueAt} />}
        </Row>
      );
  }
}
