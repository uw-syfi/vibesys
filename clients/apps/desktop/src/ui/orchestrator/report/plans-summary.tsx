import {PlanStatus} from '@vibesys/orchestrator-proto';
import {Card, Chip, Row, type StatusTone, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunReport} from '../../../state/index.js';

/** The plans view of a report, once a search state has been served. */
type PlansView = NonNullable<RunReport['plans']>;

/** The tone each plan status wears: open work, a plan chosen, won, or set aside. */
const STATUS_TONES = {
  [PlanStatus.UNSPECIFIED]: 'neutral',
  [PlanStatus.OPEN]: 'warn',
  [PlanStatus.CHOSEN]: 'live',
  [PlanStatus.WON]: 'ok',
  [PlanStatus.PARKED]: 'neutral',
  [PlanStatus.REJECTED]: 'neutral',
} as const satisfies Record<PlanStatus, StatusTone>;

/** Says how many jobs the host ran that no run has claimed, with the right plural. */
const withoutRunText = (count: number): string =>
  `${count} ${count === 1 ? 'job' : 'jobs'} with no run`;

/**
 * The run's plans at a glance: the host's count of them by status, each a chip in its tone, and how
 * many jobs the host ran that no run has claimed. Chosen and Won are kept apart here, which the
 * ideas table's verdict folds together. Every word and figure is the host's; this card only lays
 * them out and colors each status.
 */
export function PlansSummary(props: {readonly plans: PlansView}): JSX.Element {
  const {plans} = props;
  return (
    <Card dense section="Plan status">
      <Row gap="space-4" rowGap="space-3" wrap align="center" justify="between">
        <Row gap="space-2" rowGap="space-2" wrap align="center">
          {plans.tallies.length === 0 ? (
            <Text tone="muted">No plans</Text>
          ) : (
            plans.tallies.map(tally => (
              <Chip
                key={tally.status}
                label={`${tally.label} ${tally.count}`}
                tone={STATUS_TONES[tally.status]}
              />
            ))
          )}
        </Row>
        <Text variant="small" tone="muted">
          {withoutRunText(plans.withoutRun)}
        </Text>
      </Row>
    </Card>
  );
}
