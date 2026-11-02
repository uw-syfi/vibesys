import type {Plan} from '@vibesys/orchestrator-proto';
import {Card, EmptyState, Stack, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {PLAN_ORDER, planWord} from './words.js';

export type IdeasLoad =
  | {readonly status: 'idle'}
  | {readonly status: 'loading'}
  | {readonly status: 'ready'; readonly plans: readonly Plan[]}
  | {readonly status: 'failed'; readonly error: string};

const COLUMNS: readonly TableColumn<Plan>[] = [
  {key: 'number', header: 'No.', mono: true, cell: plan => String(plan.number)},
  {key: 'title', header: 'Idea', lead: true, cell: plan => plan.title},
  {
    key: 'comments',
    header: 'Notes',
    mono: true,
    align: 'end',
    wide: true,
    cell: plan => String(plan.comments.length),
  },
];

/** The run's ideas grouped by where they stand. Read only. */
export function IdeasView(props: {readonly load: IdeasLoad}): JSX.Element {
  const {load} = props;
  if (load.status === 'idle' || load.status === 'loading')
    return <Text tone="muted">Loading ideas.</Text>;
  if (load.status === 'failed') {
    return <EmptyState title="Could not load the ideas" body={load.error} />;
  }
  if (load.plans.length === 0) {
    return (
      <EmptyState title="No ideas yet" body="Planners write ideas here as they think of them." />
    );
  }
  return (
    <Stack gap="space-4">
      {PLAN_ORDER.map(status => {
        const plans = load.plans.filter(plan => plan.status === status);
        if (plans.length === 0) return null;
        const word = planWord(status);
        return (
          <Card key={status} section={`${word.label} (${plans.length})`}>
            <Table
              label={`Ideas: ${word.label}`}
              columns={COLUMNS}
              rows={plans}
              rowKey={plan => String(plan.number)}
            />
          </Card>
        );
      })}
    </Stack>
  );
}
