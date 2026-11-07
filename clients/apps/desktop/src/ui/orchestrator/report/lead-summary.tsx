import {Card, Chip, KeyValue, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {SummaryField, SummaryView} from '../../../state/index.js';
import {withShortCommits} from '../../commit-text.js';
import {useDisplayClock} from '../../display-clock.js';
import {formatAge} from './format.js';

const FIELD_LABELS: Readonly<Record<SummaryField, string>> = {
  changed: 'Changed',
  inFlight: 'Running',
  decisions: 'Decided',
};

/**
 * The lead's latest report as the run's summary, with how long ago it was written. A run whose lead
 * has not reported says only that. What the lead wrote is its own words and wraps.
 */
export function LeadSummary(props: {
  readonly summary: SummaryView | undefined;
  readonly nowMs: number;
}): JSX.Element {
  const {summary} = props;
  const clock = useDisplayClock();
  const rest = (summary?.fields ?? []).map(({field, text}) => ({
    key: FIELD_LABELS[field],
    value: withShortCommits(text),
  }));
  return (
    <Card section="Lead summary">
      {summary === undefined ? (
        <Text tone="muted">No report yet</Text>
      ) : (
        <Stack gap="space-3">
          <Row gap="space-3" wrap>
            <Text variant="small" tone="muted" title={clock.reportMoment(summary.atMs)}>
              {`Updated ${formatAge(props.nowMs - summary.atMs)}`}
            </Text>
            {summary.final ? <Chip label="Final" tone="ok" /> : null}
          </Row>
          <Text as="p">{withShortCommits(summary.headline)}</Text>
          {rest.length === 0 ? null : <KeyValue items={rest} />}
        </Stack>
      )}
    </Card>
  );
}
