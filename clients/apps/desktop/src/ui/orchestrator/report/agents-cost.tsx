import {Button, Card, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type AgentCostRow,
  type AgentsStrip,
  agentCostRows,
  type CostFeedSnapshot,
  type CostView,
} from '../../../state/index.js';
import {
  AgentTypeTable,
  agentTypeLine,
  FLOOR,
  formatCount,
  NOT_REPORTED,
  precisionOf,
} from '../../cost/index.js';
import {plural, UNKNOWN} from '../../plain-words.js';
import {faultWord, roleWord} from '../words.js';
import {yet} from './yet.js';

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
 * What the table's figures leave unsaid: how many turns there were, and what an unknown, a dash or a ≥
 * in the Cost column means. Each sentence is there only when the table has such a figure.
 */
function CostNotes(props: {
  readonly cost: CostView;
  readonly rows: readonly AgentCostRow[];
}): JSX.Element {
  const {cost, rows} = props;
  const known = (row: AgentCostRow): boolean => row.spend !== null;
  const unknown = rows.some(
    row => known(row) && row.spend?.usage.costUsd === null && row.spend.turns > 0,
  );
  const nothingYet = rows.some(row => row.spend?.turns === 0);
  const floor = rows.some(
    row => row.spend?.usage.costUsd !== null && (row.spend?.unpricedTurns ?? 0) > 0,
  );
  const notes = [
    cost.unpricedTurns === 0
      ? `${formatCount(cost.turns)} ${plural(cost.turns, 'turn', 'turns')}.`
      : `${formatCount(cost.unpricedTurns)} of ${formatCount(cost.turns)} turns reported no price.`,
    floor ? `${FLOOR.trim()} marks a cost that is at least this.` : '',
    unknown ? `${UNKNOWN} marks a type none of whose turns was priced.` : '',
    nothingYet ? `${NOT_REPORTED} marks a type with no turn yet.` : '',
  ].filter(note => note !== '');
  return (
    <Text variant="small" tone="muted">
      {notes.join(' ')}
    </Text>
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
  /** The run has ended: no agent is still to come. */
  readonly runOver: boolean;
}): JSX.Element {
  const {agents, cost, feed} = props;
  const rows = agentCostRows(agents, cost);
  const precision = precisionOf(cost?.total.costUsd ?? null);
  return (
    <Card
      dense
      section="Agents and cost"
      action={
        <Row gap="space-1">
          <Button
            text="Open agents"
            end="chevron-right"
            variant="quiet"
            size="sm"
            press={props.openAgents}
          />
          <Button
            text="Open cost"
            end="chevron-right"
            variant="quiet"
            size="sm"
            press={props.open}
          />
        </Row>
      }
    >
      <Stack gap="space-3">
        <AgentTypeTable
          label="Agents and cost by type"
          rows={rows.map(row =>
            agentTypeLine({
              key: String(row.role),
              name: roleWord(row.role),
              running: row.running,
              total: row.total,
              spend: row.spend,
              precision,
            }),
          )}
          empty={yet('No agents', props.runOver)}
        />
        {cost === undefined ? null : <CostNotes cost={cost} rows={rows} />}
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
