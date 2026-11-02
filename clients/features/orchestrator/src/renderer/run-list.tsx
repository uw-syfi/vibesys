import type {Run} from '@vibesys/orchestrator-proto';
import {ms, type OrchestratorState} from '@vibesys/orchestrator-state';
import {Chip, EmptyState, Stack, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {formatClock, runWord} from './words.js';

/** One connected host and the runs it has told us about. */
export interface HostRuns {
  readonly host: string;
  /** `waiting` while the connection is down and being redialled. */
  readonly connection: 'live' | 'waiting';
  readonly state: OrchestratorState;
}

interface RunRow {
  readonly host: string;
  readonly run: Run;
  readonly agents: number;
}

const rowsOf = (hosts: readonly HostRuns[]): RunRow[] =>
  hosts
    .flatMap(({host, state}) =>
      Object.values(state.runs).map(run => ({
        host,
        run,
        agents: Object.values(state.agents).filter(agent => agent.runId === run.id).length,
      })),
    )
    .sort((a, b) => (ms(b.run.createdAt) ?? 0) - (ms(a.run.createdAt) ?? 0));

const COLUMNS: readonly TableColumn<RunRow>[] = [
  {key: 'task', header: 'Task', lead: true, cell: row => row.run.task},
  {
    key: 'status',
    header: 'Status',
    cell: row => {
      const word = runWord(row.run.status);
      return <Chip label={word.label} tone={word.tone} dot />;
    },
  },
  {key: 'agents', header: 'Agents', mono: true, align: 'end', cell: row => String(row.agents)},
  {key: 'host', header: 'Host', mono: true, wide: true, cell: row => row.host},
  {
    key: 'started',
    header: 'Started (UTC)',
    mono: true,
    wide: true,
    cell: row => formatClock(ms(row.run.createdAt) ?? 0),
  },
];

/** Runs across every connected host, newest first. */
export function RunList(props: {
  readonly hosts: readonly HostRuns[];
  readonly open: (host: string, runId: string) => void;
}): JSX.Element {
  const rows = rowsOf(props.hosts);
  const down = props.hosts.filter(host => host.connection === 'waiting');
  return (
    <Stack gap="space-4">
      {down.map(host => (
        <Text key={host.host} tone="muted">
          {`Lost the connection to ${host.host}. Trying again.`}
        </Text>
      ))}
      {rows.length === 0 ? (
        <EmptyState
          title="No runs yet"
          body="Runs started on a connected host appear here as soon as the host reports them."
        />
      ) : (
        <Table
          label="Runs on all connected hosts"
          columns={COLUMNS}
          rows={rows}
          rowKey={row => `${row.host}/${row.run.id}`}
          select={key => {
            const row = rows.find(candidate => `${candidate.host}/${candidate.run.id}` === key);
            if (row !== undefined) props.open(row.host, row.run.id);
          }}
        />
      )}
    </Stack>
  );
}
