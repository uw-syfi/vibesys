import {Chip, LabelledLine, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {SummaryField, SummaryView} from '../../../state/index.js';
import {withShortCommits} from '../../commit-text.js';
import {formatAge} from './format.js';

const FIELD_LABELS: Readonly<Record<SummaryField, string>> = {
  changed: 'Changed',
  inFlight: 'Running',
  decisions: 'Decided',
};

/**
 * The lead's latest report as a labelled row under the facts, not a card: the label column holds
 * how long ago it was written (and that it is the final one), then the lead's own words (the
 * headline, then the parts it filled in, in columns). A run whose lead has not reported says only
 * that, in one line. What the lead wrote is its own words and wraps.
 */
export function LeadSummary(props: {
  readonly summary: SummaryView | undefined;
  readonly nowMs: number;
}): JSX.Element {
  const {summary} = props;
  if (summary === undefined) {
    return (
      <LabelledLine
        label="Lead"
        lead={
          <Text tone="muted" variant="small">
            No report yet
          </Text>
        }
      />
    );
  }
  return (
    <LabelledLine
      label="Lead"
      note={formatAge(props.nowMs - summary.atMs)}
      {...(summary.final ? {badge: <Chip label="Final" tone="ok" />} : {})}
      lead={<Text as="p">{withShortCommits(summary.headline)}</Text>}
      fields={summary.fields.map(({field, text}) => ({
        label: FIELD_LABELS[field],
        text: withShortCommits(text),
      }))}
    />
  );
}
