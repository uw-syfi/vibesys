import type {Run} from '@vibesys/orchestrator-proto';
import {ms, type OrchestratorState} from '@vibesys/orchestrator-state';
import {Chip, EmptyState, Stack, Table, type TableColumn} from '@vibesys/ui';
import type {JSX} from 'react';
import {formatClock, runWord} from './words.js';

/** One connected host and the runs it has told us about. */
export interface HostRuns {
  /** `HostKey` of the host: what a route names. */
  readonly key: string;
  /** What the host is called on screen. */
  readonly label: string;
  readonly state: OrchestratorState;
}

interface RunRow {
  readonly key: string;
  readonly host: string;
  readonly run: Run;
  readonly agents: number;
}

const rowsOf = (hosts: readonly HostRuns[]): RunRow[] =>
  hosts
    .flatMap(({key, label, state}) =>
      Object.values(state.runs).map(run => ({
        key,
        host: label,
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
  readonly open: (hostKey: string, runId: string) => void;
}): JSX.Element {
  const rows = rowsOf(props.hosts);
  return (
    <Stack gap="space-4">
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
          rowKey={row => `${row.key}/${row.run.id}`}
          select={key => {
            const row = rows.find(candidate => `${candidate.key}/${candidate.run.id}` === key);
            if (row !== undefined) props.open(row.key, row.run.id);
          }}
        />
      )}
    </Stack>
  );
}
