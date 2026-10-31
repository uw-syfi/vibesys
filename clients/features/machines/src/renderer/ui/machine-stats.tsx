/**
 * A machine's live stats on its card: gauges for CPU, memory and the GPUs, dimmed with the age of
 * the value when the host stopped delivering, or one status word and a Retry when there is nothing
 * to show. Labels, numbers and status words only.
 */
import {Button, Gauge, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {StatsView} from '../../state/index.js';

export function MachineStats(props: {
  readonly view: StatsView;
  /** How old a stale value is, as words; null while the value is current. */
  readonly age: string | null;
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
          {props.age === null ? null : (
            <Text variant="small" tone="muted">
              {`Stale ${props.age}`}
            </Text>
          )}
        </Row>
      );
  }
}
