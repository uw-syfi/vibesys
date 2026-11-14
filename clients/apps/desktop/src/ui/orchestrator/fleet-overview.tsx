import {Chip, Row, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {FleetOverview} from '../../state/index.js';
import {dollars} from './fleet-words.js';

/**
 * A summary of the live fleet, above the run list: how many live runs want the operator, the agents
 * working across them and their spend. A reading of the rows shown, so it answers the same All/Live/
 * Ended filter and search as the list below it. The live count itself is not repeated here — the
 * "Live" section header and tab already carry it. The attention count is the one thing to act on, so
 * it alone is a chip; the agents and spend are quiet stats. A figure the hosts did not report is left
 * out rather than shown as a real zero, and the whole band is hidden when there is nothing live to say.
 */
export function FleetOverviewBar(props: {readonly overview: FleetOverview}): JSX.Element | null {
  const {attention, agentsWorking, liveCostUsd} = props.overview;
  if (attention === 0 && agentsWorking === null && liveCostUsd === null) return null;
  return (
    <section aria-label="Live fleet">
      <Row gap="space-3" align="center" wrap>
        {attention > 0 ? (
          <Chip
            label={`${attention} need attention`}
            tone="warn"
            dot
            hint="Live runs with a stall, stuck jobs, a limit reached, kept checkouts, or an unreadable report"
          />
        ) : null}
        {agentsWorking === null ? null : (
          <Text variant="small" tone="muted">
            {`${agentsWorking} ${agentsWorking === 1 ? 'agent' : 'agents'} working`}
          </Text>
        )}
        {liveCostUsd === null ? null : (
          <Text variant="small" tone="muted">{`${dollars(liveCostUsd)} live spend`}</Text>
        )}
      </Row>
    </section>
  );
}
