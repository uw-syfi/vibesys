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
  type UsageView,
} from '../../state/index.js';

/** A run of a project on a machine, as a row of the page names it. */
export interface SpendRunRef {
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
}

export interface UsageScreenProps {
  readonly state: UsageScreenState;
  readonly actions: UsageActions;
  /** Open the run's Usage face. */
  readonly openRun: (run: SpendRunRef) => void;
}

const RUN_COLUMNS: readonly TableColumn<RunSpendRow>[] = [
  {
    key: 'run',
    header: 'Run',
    lead: true,
    cell: row => (
      <Stack gap="space-0">
        <Text variant="body-strong">{row.name}</Text>
        {row.detail === null ? null : (
          <Text variant="small" tone="muted">
            {row.detail}
          </Text>
        )}
      </Stack>
    ),
  },
  {
    key: 'share',
    header: 'Share',
    cell: row => (
      <Meter label={`${row.detail ?? row.name}: ${row.cost}`} value={row.share} inline />
    ),
  },
  {key: 'cost', header: 'Cost', mono: true, align: 'end', cell: row => row.cost},
];

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
  {key: 'turns', header: 'Turns', mono: true, align: 'end', wide: true, cell: row => row.turns},
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
}): JSX.Element {
  const {view, openRun} = props;
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
            <Chip key={each.key} label={each.label} tone="warn" />
          ))}
        </Row>
      )}
      <Section title="Cost by run">
        <Table
          label="Cost by run"
          columns={RUN_COLUMNS}
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
  const {state, actions, openRun} = props;
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
  return <Figures view={state.view} openRun={openRun} />;
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
