/**
 * The Usage page: what every run spent in a window. A window select and Refresh above four tiles,
 * then the cost of each run (a row opens that run) and of each kind of agent, as rows with a bar
 * against the costliest. Labels, figures and status words only. Render is a function of the store's
 * state; the only presses are the window, Refresh and a run's row.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  Button,
  Chip,
  EmptyState,
  Field,
  LoadingBar,
  Meter,
  MetricTile,
  MetricTiles,
  Page,
  Row,
  Select,
  Stack,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type AgentSpendRow,
  type RunSpendRow,
  USAGE_WINDOWS,
  type UsageActions,
  type UsageScreenState,
  type UsageStatus,
  type UsageView,
} from '../../state/index.js';

/** A run of a project on a machine, as a row of the page names it. */
export interface SpendRunRef {
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
}

/** What the page calls a run: its title as every screen words it, and when it began if known. */
export interface SpendRunLabel {
  readonly title: string;
  readonly began: string | null;
}

export interface UsageScreenProps {
  readonly state: UsageScreenState;
  readonly actions: UsageActions;
  /** What a run's row is called; the shell owns run titles. */
  readonly label: (run: SpendRunRef) => SpendRunLabel;
  /** Open the run's Usage face. */
  readonly openRun: (run: SpendRunRef) => void;
}

function runColumns(
  label: (run: SpendRunRef) => SpendRunLabel,
): readonly TableColumn<RunSpendRow>[] {
  return [
    {
      key: 'run',
      header: 'Run',
      lead: true,
      cell: row => {
        const {title, began} = label(row);
        return (
          <Stack gap="space-0">
            <Text variant="body-strong">{title}</Text>
            {began === null ? null : (
              <Text variant="small" tone="muted">
                {began}
              </Text>
            )}
          </Stack>
        );
      },
    },
    {
      key: 'share',
      header: 'Share',
      cell: row => {
        const {title, began} = label(row);
        return (
          <Meter
            label={`${began === null ? title : `${title} ${began}`}: ${row.cost}`}
            value={row.share}
            inline
          />
        );
      },
    },
    {
      key: 'cost',
      header: 'Cost',
      mono: true,
      align: 'end',
      cell: row => (
        <Row gap="space-2" align="center" justify="end">
          {row.unpriced === 0 ? null : <Chip label={`${row.unpriced} unpriced`} tone="warn" />}
          {row.cost}
        </Row>
      ),
    },
  ];
}

const AGENT_COLUMNS: readonly TableColumn<AgentSpendRow>[] = [
  {
    key: 'role',
    header: 'Role',
    lead: true,
    cell: row => <Text variant="body-strong">{row.role}</Text>,
  },
  {key: 'harness', header: 'Harness', cell: row => row.harness},
  {
    key: 'model',
    header: 'Model',
    cell: row => (
      <span title={row.model.id ?? undefined}>
        <Text variant={row.model.id === null ? 'code' : 'body'}>{row.model.name}</Text>
      </span>
    ),
  },
  {
    key: 'turns',
    header: 'Turns',
    mono: true,
    align: 'end',
    wide: true,
    cell: row => (
      <Row gap="space-2" align="center" justify="end">
        {row.unpriced === 0 ? null : <Chip label={`${row.unpriced} unpriced`} tone="warn" />}
        {row.turns}
      </Row>
    ),
  },
  {
    key: 'share',
    header: 'Share',
    wide: true,
    cell: row => (
      <Meter
        label={`${row.role} ${row.harness} ${row.model.name}: ${row.cost}`}
        value={row.share}
        inline
      />
    ),
  },
  {key: 'cost', header: 'Cost', mono: true, align: 'end', cell: row => row.cost},
];

/** What a status counts, a line per part, for the pointer: a run by the title every screen gives it. */
function statusHint(
  status: UsageStatus,
  label: (run: SpendRunRef) => SpendRunLabel,
): string | undefined {
  const lines = [
    ...status.runs.map(run => {
      const {title, began} = label(run);
      const name = began === null ? title : `${title} ${began}`;
      return status.key === 'not-counted' ? `${name} ${run.runId}` : `${name}: ${run.count}`;
    }),
    ...status.parts,
    ...status.notes,
  ];
  return lines.length === 0 ? undefined : lines.join('\n');
}

function Section(props: {readonly title: string; readonly children: JSX.Element}): JSX.Element {
  return (
    <Stack gap="space-3">
      <Text as="h2" variant="section">
        {props.title}
      </Text>
      {props.children}
    </Stack>
  );
}

function Figures(props: {
  readonly view: UsageView;
  readonly openRun: (run: SpendRunRef) => void;
  readonly label: (run: SpendRunRef) => SpendRunLabel;
}): JSX.Element {
  const {view, openRun, label} = props;
  if (view.empty) return <EmptyState title="No usage" />;
  const runs = new Map(view.runs.map(row => [row.key, row]));
  return (
    <Stack gap="space-5">
      <MetricTiles label="Totals" count={4}>
        {view.tiles.map(tile => (
          <MetricTile
            key={tile.name}
            name={tile.name}
            figure={tile.figure}
            {...(tile.detail === null ? {} : {detail: tile.detail})}
          />
        ))}
      </MetricTiles>
      {view.status.length === 0 ? null : (
        <Row gap="space-2" wrap>
          {view.status.map(each => (
            <span key={each.key} title={statusHint(each, label)}>
              <Chip label={each.label} tone="warn" />
            </span>
          ))}
        </Row>
      )}
      <Section title="Cost by run">
        <Table
          label="Cost by run"
          columns={runColumns(label)}
          rows={view.runs}
          rowKey={row => row.key}
          select={key => {
            const run = runs.get(key);
            if (run !== undefined)
              openRun({host: run.host, project: run.project, runId: run.runId});
          }}
        />
      </Section>
      <Section title="Cost by agent">
        <Stack gap="space-2">
          <Table
            label="Cost by agent"
            columns={AGENT_COLUMNS}
            rows={view.agents}
            rowKey={row => row.key}
          />
          {view.agentsOmitted === 0 ? null : (
            <Text variant="small" tone="muted">{`${view.agentsOmitted} more`}</Text>
          )}
        </Stack>
      </Section>
    </Stack>
  );
}

function Body(props: UsageScreenProps): JSX.Element {
  const {state, actions, openRun, label} = props;
  if (state.phase === 'unsupported') return <EmptyState title="Unsupported" />;
  if (state.phase === 'failed') {
    return (
      <EmptyState
        title="Unavailable"
        action={
          <Button
            text="Retry"
            variant="secondary"
            size="sm"
            press={actions.refresh}
            {...(state.problem === null ? {} : {hint: state.problem})}
          />
        }
      />
    );
  }
  if (state.view === null) {
    return (
      <Stack gap="space-3">
        <LoadingBar label="Reading usage" />
        <Text tone="muted">Loading</Text>
      </Stack>
    );
  }
  return <Figures view={state.view} openRun={openRun} label={label} />;
}

export function UsageScreen(props: UsageScreenProps): JSX.Element {
  const {state, actions} = props;
  return (
    <Page width="wide">
      <Stack gap="space-5">
        <Row justify="between" align="center" wrap>
          <Text as="h1" variant="title">
            Usage
          </Text>
          <Row gap="space-3" align="center">
            {state.phase === 'loading' && state.view !== null ? (
              <Text variant="small" tone="muted">
                Loading
              </Text>
            ) : null}
            <Field label="Window" hideLabel>
              <Select
                size="sm"
                value={state.window}
                options={USAGE_WINDOWS}
                change={actions.choose}
              />
            </Field>
            <Button text="Refresh" variant="secondary" size="sm" press={actions.refresh} />
          </Row>
        </Row>
        <Body {...props} />
      </Stack>
    </Page>
  );
}
