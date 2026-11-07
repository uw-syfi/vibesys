import {Card, Chip, KeyValue, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {SummaryField, SummaryView} from '../../../state/index.js';
import {formatAge, seattleMoment} from './format.js';

const FIELD_LABELS: Readonly<Record<Exclude<SummaryField, 'openForUser'>, string>> = {
  changed: 'Changed',
  inFlight: 'Running',
  decisions: 'Decided',
};

/**
 * The lead's latest report as the run's summary, with how long ago it was written. A run whose lead
 * has not reported says only that. What the lead wrote is its own words and wraps. What the lead
 * needs from the person is set apart from the rest in the warning tone, because it is the one line
 * that asks something of the reader.
 */
export function LeadSummary(props: {
  readonly summary: SummaryView | undefined;
  readonly nowMs: number;
}): JSX.Element {
  const {summary} = props;
  const ask = summary?.fields.find(({field}) => field === 'openForUser');
  const rest = (summary?.fields ?? []).flatMap(({field, text}) =>
    field === 'openForUser' ? [] : [{key: FIELD_LABELS[field], value: text}],
  );
  return (
    <Card section="Lead summary">
      {summary === undefined ? (
        <Text tone="muted">No report yet</Text>
      ) : (
        <Stack gap="space-3">
          <Row gap="space-3" wrap>
            <Text variant="small" tone="muted" title={seattleMoment(summary.atMs)}>
              {`Updated ${formatAge(props.nowMs - summary.atMs)}`}
            </Text>
            {summary.final ? <Chip label="Final" tone="ok" /> : null}
          </Row>
          <Text as="p">{summary.headline}</Text>
          {ask === undefined ? null : (
            <Row gap="space-3" align="start" wrap>
              <Chip label="Needs you" tone="warn" dot />
              <Text variant="body-strong">{ask.text}</Text>
            </Row>
          )}
          {rest.length === 0 ? null : <KeyValue items={rest} />}
        </Stack>
      )}
    </Card>
  );
}
