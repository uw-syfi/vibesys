import type {AgentRole} from '@vibesys/orchestrator-proto';
import {Card, Row, Stack, Stat, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {type OrchestratorState, runAgents, type UsageTotals, usageOf} from '../../state/index.js';
import {agentNames, nameOf} from './names.js';
import {formatCost, formatTokens, roleWord} from './words.js';

interface Line {
  readonly key: string;
  readonly name: string;
  readonly usage: UsageTotals;
}

const COLUMNS: readonly TableColumn<Line>[] = [
  {key: 'name', header: 'Name', lead: true, cell: line => line.name},
  {key: 'turns', header: 'Turns', mono: true, align: 'end', cell: line => String(line.usage.turns)},
  {
    key: 'in',
    header: 'Read (tokens)',
    mono: true,
    align: 'end',
    cell: line => formatTokens(line.usage.inputTokens),
  },
  {
    key: 'out',
    header: 'Written (tokens)',
    mono: true,
    align: 'end',
    cell: line => formatTokens(line.usage.outputTokens),
  },
  {
    key: 'cache',
    header: 'Reused (tokens)',
    mono: true,
    align: 'end',
    wide: true,
    cell: line => formatTokens(line.usage.cacheReadTokens),
  },
  {
    key: 'cost',
    header: 'Reported cost',
    mono: true,
    align: 'end',
    cell: line => formatCost(line.usage.costUsd),
  },
];

/** What the run has used, by role, by agent, and in all. */
export function CostView(props: {
  readonly state: OrchestratorState;
  readonly runId: string;
}): JSX.Element {
  const usage = usageOf(props.state, props.runId);
  const names = agentNames(props.state, props.runId);
  const roles: Line[] = (Object.keys(usage.byRole) as unknown as string[])
    .map(Number)
    .map(role => ({
      key: `role-${role}`,
      name: `All ${roleWord(role as AgentRole).toLowerCase()}s`,
      usage: usage.byRole[role as AgentRole] ?? usage.run,
    }));
  const agents: Line[] = runAgents(props.state, props.runId).map(agent => ({
    key: agent.id,
    name: nameOf(names, agent),
    usage: usage.byAgent[agent.id] ?? usage.run,
  }));
  return (
    <Stack gap="space-6">
      <Row gap="space-6" wrap>
        <Stat label="Reported cost" value={formatCost(usage.run.costUsd)} />
        <Stat label="Tokens read" value={formatTokens(usage.run.inputTokens)} />
        <Stat label="Tokens written" value={formatTokens(usage.run.outputTokens)} />
      </Row>
      <Text tone="muted" variant="small">
        A token is a small piece of text, roughly three quarters of a word; agents are billed by the
        tokens they read and write. Some tools do not report a cost or a count. Those show "Not
        reported" and are left out of the totals, never counted as zero, so the cost is only what
        was reported.
      </Text>
      <Card section="By role">
        <Table label="Usage by role" columns={COLUMNS} rows={roles} rowKey={line => line.key} />
      </Card>
      <Card section="By agent">
        <Table label="Usage by agent" columns={COLUMNS} rows={agents} rowKey={line => line.key} />
      </Card>
    </Stack>
  );
}
