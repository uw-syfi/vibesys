import {Button, Card, Row, Stack, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type AgentCostRow,
  type AgentsStrip,
  agentCostRows,
  type CostFeedSnapshot,
  type CostView,
} from '../../../state/index.js';
import {
  FLOOR,
  formatCount,
  formatTokens,
  NOT_REPORTED,
  precisionOf,
  usdOrNothing,
} from '../../cost/index.js';
import {faultWord, roleWord} from '../words.js';

/** The columns of the agents-and-cost table: the type, who is working, and what the type has cost. */
function columns(precision: ReturnType<typeof precisionOf>): TableColumn<AgentCostRow>[] {
  return [
    {key: 'type', header: 'Type', lead: true, cell: row => roleWord(row.role)},
    {
      key: 'running',
      header: 'Running',
      mono: true,
      align: 'end',
      cell: row => `${row.running} of ${row.total}`,
    },
    {
      key: 'turns',
      header: 'Turns',
      mono: true,
      align: 'end',
      cell: row => (row.turns === null ? NOT_REPORTED : formatCount(row.turns)),
    },
    {
      key: 'tokens',
      header: 'Tokens',
      mono: true,
      align: 'end',
      cell: row => (row.tokens === null ? NOT_REPORTED : formatTokens(row.tokens)),
    },
    {
      key: 'cost',
      header: 'Cost',
      mono: true,
      align: 'end',
      cell: row => {
        const usd = usdOrNothing(row.costUsd, precision);
        if (usd === null) return NOT_REPORTED;
        return row.unpricedTurns > 0 ? `${FLOOR}${usd}` : usd;
      },
    },
  ];
}

/** What went wrong with the cost stream, beside the figures it last delivered, and the way to retry. */
function FeedProblem(props: {
  readonly feed: CostFeedSnapshot;
  readonly retry: () => void;
}): JSX.Element | null {
  const {status} = props.feed;
  if (status._tag !== 'Failed') return null;
  return (
    <Row gap="space-3" wrap>
      <Text tone="warn" variant="small">
        {faultWord(status.fault.kind)}
      </Text>
      {status.fault.retryable ? (
        <Button text="Retry" label="Retry cost" variant="secondary" size="sm" press={props.retry} />
      ) : null}
    </Row>
  );
}

/**
 * The agents of the run by type with what each type has cost: how many are working of how many the
 * run has had, their turns, tokens and cost, every figure the host's. The spend against its cap is the
 * facts line's and the Cost page's; the links in the heading open that page and the Agents page. A cost that is not read
 * yet shows dashes and says so; one that cannot be read says why, beside the figures it last delivered.
 */
export function AgentsAndCost(props: {
  readonly agents: AgentsStrip;
  readonly cost: CostView | undefined;
  readonly feed: CostFeedSnapshot;
  readonly retry: () => void;
  /** Open the Agents page of the run. */
  readonly openAgents: () => void;
  /** Open the Cost page of the run. */
  readonly open: () => void;
}): JSX.Element {
  const {agents, cost, feed} = props;
  const rows = agentCostRows(agents, cost);
  return (
    <Card
      dense
      section="Agents and cost"
      action={
        <Row gap="space-1">
          <Button
            text="Agents"
            label="Open agents"
            variant="quiet"
            size="sm"
            press={props.openAgents}
          />
          <Button text="Cost" label="Open cost" variant="quiet" size="sm" press={props.open} />
        </Row>
      }
    >
      <Stack gap="space-3">
        <Table
          label="Agents and cost by type"
          dense
          middle
          rows={rows}
          rowKey={row => String(row.role)}
          columns={columns(precisionOf(cost?.total.costUsd ?? null))}
          empty="No agents yet"
        />
        {cost === undefined && feed.status._tag !== 'Failed' ? (
          <Text tone="muted" variant="small">
            Reading cost
          </Text>
        ) : null}
        <FeedProblem feed={feed} retry={props.retry} />
      </Stack>
    </Card>
  );
}
