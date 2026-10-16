import {
  Card,
  Chip,
  EmptyState,
  Field,
  Row,
  SegmentedControl,
  Stack,
  Table,
  type TableColumn,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type ProgressNote,
  type RunAtView,
  type RunPageAction,
  type WorkFilter,
  type WorkOutcome,
  type WorkRow,
  workView,
} from '../../state/index.js';

const OUTCOMES: readonly {readonly value: WorkOutcome; readonly label: string}[] = [
  {value: 'all', label: 'All'},
  {value: 'accepted', label: 'Accepted'},
  {value: 'rejected', label: 'Rejected'},
  {value: 'running', label: 'Running'},
];

const WORKSTREAM_COLUMNS: readonly TableColumn<WorkRow>[] = [
  {key: 'id', header: 'ID', cell: row => row.id, mono: true},
  {key: 'hypothesis', header: 'Hypothesis', cell: row => row.hypothesis},
  {key: 'builds-on', header: 'Builds on', cell: row => row.buildsOn, mono: true},
  {key: 'turns', header: 'Agent turns', cell: row => row.turns, mono: true},
  {key: 'gate', header: 'Gate', cell: row => row.gate, mono: true},
  {
    key: 'outcome',
    header: 'Outcome',
    cell: row => <Chip label={row.outcome.label} tone={row.outcome.tone} />,
  },
  {key: 'took', header: 'Took', cell: row => row.took, mono: true, align: 'end'},
];

/** A run whose search labels nothing shows its flows by what their spans say. */
const FLOW_COLUMNS: readonly TableColumn<WorkRow>[] = [
  {key: 'id', header: 'Flow', cell: row => row.id, mono: true},
  {
    key: 'outcome',
    header: 'Status',
    cell: row => <Chip label={row.outcome.label} tone={row.outcome.tone} />,
  },
  {key: 'took', header: 'Took', cell: row => row.took, mono: true, align: 'end'},
];

/**
 * The Work tab: every workstream of the run at the view (`run`), filtered by the tab's `work`
 * slice of the page state, which the search box and the outcome control change through `dispatch`.
 */
export function WorkTab(props: {
  readonly run: RunAtView;
  readonly work: WorkFilter;
  /** What the run is doing before it has any work, shown in place of "No work yet". */
  readonly progress: ProgressNote | null;
  /** The run clock's time now, for the ages of open work. */
  readonly now: number | null;
  readonly dispatch: (action: RunPageAction) => void;
}): JSX.Element {
  const {run, work, progress, now, dispatch} = props;
  const view = workView(run, work, now);
  const empty =
    view.total === 0
      ? (progress ?? {
          title: 'No work yet',
          body: 'Each workstream the run starts appears here, with its outcome.',
        })
      : {title: 'No work matches', body: 'Change the search or show all outcomes.'};
  return (
    <Stack gap="space-4">
      <Row gap="space-4" align="end" wrap>
        <Field label="Search work">
          <TextInput
            value={work.query}
            placeholder="Search hypotheses"
            change={query => dispatch({type: 'work-query', query})}
          />
        </Field>
        <SegmentedControl
          label="Show outcomes"
          options={OUTCOMES}
          value={work.outcome}
          change={outcome => dispatch({type: 'work-outcome', outcome})}
        />
      </Row>
      {view.rows.length === 0 ? (
        <Card>
          <EmptyState title={empty.title} body={empty.body} />
        </Card>
      ) : (
        <Table
          label="Workstreams"
          columns={view.labelled ? WORKSTREAM_COLUMNS : FLOW_COLUMNS}
          rows={view.rows}
          rowKey={row => row.key}
        />
      )}
    </Stack>
  );
}
