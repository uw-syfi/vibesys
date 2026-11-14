/**
 * The page of a run only the host's generic run index lists: what it is optimizing, how it stands,
 * and what its agents have done, from the typed facts of its run log. The header says the run's
 * state from the index, and the state of the read beside it; the figures and the four families of
 * facts (measurements, agents, workstreams, correctness checks) are the same layout from the first
 * read to a finished run, and a family the run did not record keeps its frame and says why.
 */
import {
  Button,
  Card,
  Chip,
  type Crumb,
  FigureStrip,
  Page,
  Row,
  Stack,
  Table,
  type TableColumn,
  Text,
  withoutPaths,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunDetailDelivery} from '../../../core/index.js';
import {
  type Fault,
  type GenericRunEntry,
  presentRunDetail,
  type RunDetailPresentation,
} from '../../../state/index.js';
import {genericIndexWords, genericRunTitle, genericRunWord} from '../generic-run-words.js';
import {faultHint, faultText} from '../words.js';
import {
  type AgentRow,
  type CheckRow,
  type MeasureRow,
  overviewOf,
  type SectionView,
  type WorkstreamRow,
} from './overview.js';

const MEASURE_COLUMNS: readonly TableColumn<MeasureRow>[] = [
  {key: 'name', header: 'Measure', lead: true, clip: row => row.name, cell: row => row.name},
  {key: 'role', header: 'Kind', fit: true, cell: row => row.role},
  {
    key: 'latest',
    header: 'Latest',
    hint: 'The newest reading, with its unit',
    mono: true,
    align: 'end',
    cell: row => (
      <Text variant="code" {...(row.hint === '' ? {} : {title: row.hint})}>
        {row.latest}
      </Text>
    ),
  },
  {
    key: 'change',
    header: 'Change',
    hint: 'From the oldest reading kept to the newest',
    mono: true,
    align: 'end',
    wide: true,
    cell: row => row.change,
  },
  {
    key: 'readings',
    header: 'Readings',
    mono: true,
    align: 'end',
    wide: true,
    cell: row => row.readings,
  },
];

const AGENT_COLUMNS: readonly TableColumn<AgentRow>[] = [
  {key: 'name', header: 'Agent', lead: true, clip: row => row.name, cell: row => row.name},
  {key: 'stage', header: 'Stage', wide: true, cell: row => row.stage},
  {
    key: 'state',
    header: 'State',
    fit: true,
    cell: row => <Chip label={row.state.label} tone={row.state.tone} dot />,
  },
  {
    key: 'model',
    header: 'Model',
    wide: true,
    mono: true,
    clip: row => row.modelHint || row.model,
    cell: row => row.model,
  },
  {
    key: 'input',
    header: 'Input',
    hint: 'Input tokens: fresh input, cache reads and cache writes',
    mono: true,
    align: 'end',
    cell: row => (
      <Text
        variant="code"
        tone="muted"
        title={`${row.tokens.cached} of the input was read from a cache`}
      >
        {row.tokens.input}
      </Text>
    ),
  },
  {
    key: 'output',
    header: 'Output',
    hint: 'Output tokens',
    mono: true,
    align: 'end',
    cell: row => row.tokens.output,
  },
  {
    key: 'cost',
    header: 'Cost',
    mono: true,
    align: 'end',
    cell: row => (
      <Text variant="code" {...(row.cost.hint === undefined ? {} : {title: row.cost.hint})}>
        {row.cost.text}
      </Text>
    ),
  },
];

const WORKSTREAM_COLUMNS: readonly TableColumn<WorkstreamRow>[] = [
  {key: 'title', header: 'Workstream', lead: true, clip: row => row.hint, cell: row => row.title},
  {
    key: 'state',
    header: 'State',
    fit: true,
    cell: row => <Chip label={row.state.label} tone={row.state.tone} dot />,
  },
  {key: 'round', header: 'Round', mono: true, align: 'end', wide: true, cell: row => row.round},
  {
    key: 'result',
    header: 'Result',
    clip: row => row.result,
    cell: row => <Text tone={row.failed ? 'err' : 'default'}>{row.result}</Text>,
  },
];

const CHECK_COLUMNS: readonly TableColumn<CheckRow>[] = [
  {
    key: 'title',
    header: 'Check',
    lead: true,
    clip: row => `${row.title}\nMeasured by: ${row.source}`,
    cell: row => row.title,
  },
  {
    key: 'check',
    header: 'Stage',
    fit: true,
    wide: true,
    cell: row => `${row.check} · ${row.subject}`,
  },
  {
    key: 'result',
    header: 'Result',
    fit: true,
    cell: row => <Text tone={row.failed ? 'err' : 'default'}>{row.result}</Text>,
  },
  {
    key: 'evidence',
    header: 'Evidence',
    wide: true,
    clip: row => (row.evidenceHint === '' ? row.evidence : row.evidenceHint),
    cell: row => row.evidence,
  },
];

/** A family of facts: its table in its frame, and under it why the host served less than the run did. */
function Family<Row>(props: {
  readonly title: string;
  readonly label: string;
  readonly view: SectionView<Row>;
  readonly columns: readonly TableColumn<Row>[];
  readonly rowKey: (row: Row) => string;
}): JSX.Element {
  const {view} = props;
  return (
    <Card dense section={props.title}>
      <Stack gap="space-3">
        <Table
          label={props.label}
          dense
          middle
          columns={props.columns}
          rows={view.rows}
          rowKey={props.rowKey}
          empty={view.empty}
        />
        {view.note === null || view.rows.length === 0 ? null : (
          <Text variant="small" tone="muted">
            {view.note}
          </Text>
        )}
      </Stack>
    </Card>
  );
}

/** The state of the read, as a chip beside the run's own state, with its retry when a press may help. */
function ReadState(props: {
  readonly delivery: RunDetailDelivery | undefined;
  readonly retry: () => void;
}): JSX.Element | null {
  const {delivery} = props;
  if (delivery === undefined || delivery._tag === 'Loading')
    return <Chip label="Reading the run" tone="neutral" />;
  if (delivery._tag === 'Live') return null;
  if (delivery._tag === 'Stale') {
    const reconnecting = delivery.why === 'disconnected' || delivery.why === 'closed';
    return (
      <Chip
        label={reconnecting ? 'Reconnecting' : 'Catching up'}
        tone="warn"
        hint="Showing what was last read; it updates when the host answers."
      />
    );
  }
  return <ProblemChip fault={delivery.fault} retry={props.retry} />;
}

function ProblemChip(props: {readonly fault: Fault; readonly retry: () => void}): JSX.Element {
  return (
    <Row gap="space-2">
      <Chip
        label={`Run detail · ${faultText(props.fault)}`}
        tone="err"
        dot
        hint={faultHint(props.fault)}
      />
      <Button
        text="Retry run detail"
        label="Retry run detail"
        variant="secondary"
        size="sm"
        press={props.retry}
      />
    </Row>
  );
}

const presentationOf = (delivery: RunDetailDelivery | undefined): RunDetailPresentation | null =>
  delivery === undefined || delivery._tag === 'Loading' || delivery.projection === null
    ? null
    : presentRunDetail(delivery.projection);

export function GenericRunScreen(props: {
  readonly run: GenericRunEntry;
  readonly freshness: 'current' | 'stale';
  /** What the run's detail feed has delivered; undefined before the window has mounted it. */
  readonly delivery: RunDetailDelivery | undefined;
  readonly retry: () => void;
  readonly trail: readonly Crumb[];
}): JSX.Element {
  const {run} = props;
  const title = genericRunTitle({projectRoot: run.projectRoot});
  const state = genericRunWord({state: run.state});
  const over = run.activity === 'ended';
  const unread = props.delivery?._tag === 'Refused' || props.delivery?._tag === 'Failed';
  const overview = overviewOf(
    run,
    presentationOf(props.delivery),
    over,
    unread ? 'unread' : 'reading',
  );
  return (
    <Page
      key="generic-run"
      width="wide"
      state="ready"
      trail={props.trail}
      title={title}
      meta={
        <Stack gap="space-2">
          <Row gap="space-2" wrap>
            <Chip label={state.label} tone={state.tone} dot />
            <Chip
              label={`Run index ${genericIndexWords({indexFreshness: props.freshness}).toLowerCase()}`}
              tone={props.freshness === 'current' ? 'neutral' : 'warn'}
            />
            {run.needsUser ? (
              <Chip
                label="Needs you"
                tone="warn"
                dot
                hint="This run is waiting for a response in the client or workflow that started it."
              />
            ) : null}
            <ReadState delivery={props.delivery} retry={props.retry} />
          </Row>
          <Row gap="space-3" rowGap="space-1" wrap>
            <Text
              variant="small"
              tone="muted"
              {...(run.projectRoot === null ? {} : {title: run.projectRoot})}
            >
              {run.projectRoot === null
                ? 'Project unavailable'
                : `Project ${withoutPaths(run.projectRoot)}`}
            </Text>
            <Text variant="code" tone="muted" title={run.runId}>
              {run.runId}
            </Text>
          </Row>
          {run.needsUser ? (
            <Text tone="warn">
              Input needed: this run is waiting for a response. Return to the client or workflow
              that started it to continue.
            </Text>
          ) : null}
        </Stack>
      }
    >
      <Stack gap="space-4">
        <FigureStrip label="The run at a glance" figures={overview.figures} />
        <Family
          title="Measurements"
          label="Measurements"
          view={overview.measures}
          columns={MEASURE_COLUMNS}
          rowKey={row => row.key}
        />
        <Family
          title="Agents"
          label="Agents"
          view={overview.agents}
          columns={AGENT_COLUMNS}
          rowKey={row => row.key}
        />
        <Family
          title="Workstreams"
          label="Workstreams"
          view={overview.workstreams}
          columns={WORKSTREAM_COLUMNS}
          rowKey={row => row.key}
        />
        <Family
          title="Correctness checks"
          label="Correctness checks"
          view={overview.checks}
          columns={CHECK_COLUMNS}
          rowKey={row => row.key}
        />
        {[overview.dropped, overview.unreadable].map(note =>
          note === null ? null : (
            <Text key={note} variant="small" tone="muted">
              {note}
            </Text>
          ),
        )}
      </Stack>
    </Page>
  );
}
