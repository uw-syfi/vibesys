import {
  Button,
  Card,
  Chip,
  Meter,
  Row,
  ShareBar,
  type SharePart,
  Stack,
  Stat,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CostCard as CostCardView, FeedStatus} from '../../../state/index.js';
import {roleWord} from '../words.js';
import {formatDollars} from './format.js';

/** How many types of agent get a place of their own in the palette before the series repeat. */
const SERIES = 8;

const dollars = (usd: number | null): string => (usd === null ? '—' : formatDollars(usd));

function partsOf(card: CostCardView): readonly SharePart[] {
  return card.types.flatMap((type, index) =>
    type.costUsd === null
      ? []
      : [
          {
            key: String(type.role),
            label: `${roleWord(type.role)} ×${type.agents}`,
            value: type.costUsd,
            figure: formatDollars(type.costUsd),
            series: (index % SERIES) + 1,
          },
        ],
  );
}

/** The totals: the cost against its limit, the last hour's, and what is wrong with either. */
function Figures(props: {readonly cost: CostCardView; readonly feed: FeedStatus}): JSX.Element {
  const {cost, feed} = props;
  return (
    <Row gap="space-5" rowGap="space-3" wrap align="end">
      <Stat
        label="Total"
        value={dollars(cost.totalUsd)}
        {...(cost.limitUsd === null ? {} : {sub: `of ${formatDollars(cost.limitUsd)}`})}
      />
      <Stat label="Last hour" value={dollars(cost.lastHourUsd)} unit="/h" />
      {cost.partial ? (
        <Chip
          label="Partial"
          tone="warn"
          hint="Some turns reported no price, so the total is a floor."
        />
      ) : null}
      {feed._tag === 'Failed' ? (
        <Chip label="Stale" tone="warn" hint="The cost stream failed. What is shown is older." />
      ) : null}
    </Row>
  );
}

/** The share of the limit used, and the split by type of agent, when there is anything to draw. */
function Shares(props: {readonly cost: CostCardView}): JSX.Element {
  const {cost} = props;
  const parts = partsOf(cost);
  return (
    <>
      {cost.limitUsd === null || cost.totalUsd === null ? null : (
        <Meter label="Cost against its limit" value={cost.totalUsd / cost.limitUsd} />
      )}
      {parts.length === 0 ? null : <ShareBar label="Cost by type of agent" parts={parts} />}
    </>
  );
}

/**
 * What the run has cost, as the host worked it out: the total against its limit, what the last hour
 * cost, and the split by type of agent. A price no turn reported is a dash, and a total that leaves
 * unpriced turns out says it is partial. A cost stream that cannot be read says so here and nowhere else.
 */
export function CostCard(props: {
  readonly cost: CostCardView | undefined;
  readonly feed: FeedStatus;
  readonly open: () => void;
}): JSX.Element {
  const {cost, feed} = props;
  return (
    <Card section="Cost">
      <Stack gap="space-4">
        {cost === undefined ? (
          <Text tone="muted">{feed._tag === 'Failed' ? 'Cost unavailable' : 'Reading cost'}</Text>
        ) : (
          <>
            <Figures cost={cost} feed={feed} />
            <Shares cost={cost} />
          </>
        )}
        <Row gap="space-3" justify="end">
          <Button text="Cost" label="Open cost" variant="quiet" size="sm" press={props.open} />
        </Row>
      </Stack>
    </Card>
  );
}
