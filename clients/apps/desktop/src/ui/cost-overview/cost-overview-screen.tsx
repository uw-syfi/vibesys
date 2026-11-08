import type {RunStatus} from '@vibesys/orchestrator-proto';
import {
  BarRows,
  Button,
  Card,
  ChartColumns,
  Chip,
  Page,
  pageState,
  Row,
  Stack,
  type StatusTone,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import type {FeedStatus, HostCostView, HostStatus} from '../../state/index.js';
import {
  type CostWords,
  MODEL_COLUMNS,
  SpendCard,
  SpendOverTime,
  TYPE_COLUMNS,
} from '../cost/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {
  type HostCostScreenModel,
  hostCostModel,
  type RunLine,
  totalFigures,
} from './cost-overview-model.js';

/** The words of the run's own vocabulary this screen needs beside the cost words. */
export interface CostOverviewWords extends CostWords {
  /** What a run's state is called, and the tone it is drawn in. */
  readonly run: (status: RunStatus) => {readonly label: string; readonly tone: StatusTone};
}

/** One host as this screen reads it. */
export interface CostOverviewHost {
  readonly key: string;
  readonly label: string;
  /** The host's one status, the same word every screen says of it. */
  readonly status: HostStatus;
  /** How the host's cost stream stands, for what went wrong with it. */
  readonly feed: FeedStatus;
  /** What the host served; undefined until it has. */
  readonly view: HostCostView | undefined;
  /** What each run the client holds a card for is called, by run id; other runs keep the host's name. */
  readonly titles: Readonly<Record<string, string>>;
}

function Problem(props: {
  readonly host: CostOverviewHost;
  readonly words: CostWords;
  readonly retry: (host: string) => void;
}): JSX.Element | null {
  const failed = props.host.feed._tag === 'Failed' ? props.host.feed.fault : null;
  if (failed === null) return null;
  return (
    <Row gap="space-3" wrap>
      <Chip label={props.words.fault(failed.kind)} tone="warn" dot />
      {failed.retryable ? (
        <Button
          text="Retry"
          label={`Retry cost of ${props.host.label}`}
          variant="secondary"
          size="sm"
          press={() => props.retry(props.host.key)}
        />
      ) : null}
    </Row>
  );
}

function runColumns(
  host: string,
  words: CostOverviewWords,
  open: (host: string, runId: string) => void,
): readonly TableColumn<RunLine>[] {
  return [
    {
      key: 'run',
      header: 'Run',
      lead: true,
      clip: line => line.name,
      cell: line => {
        const {runId} = line;
        return runId === null ? (
          <Text tone="muted">{line.name}</Text>
        ) : (
          <Button
            text={line.name}
            label={line.name}
            variant="quiet"
            size="sm"
            press={() => open(host, runId)}
          />
        );
      },
    },
    {
      key: 'status',
      header: 'Status',
      cell: line => {
        if (line.status === null) return null;
        const word = words.run(line.status);
        return <Chip label={word.label} tone={word.tone} dot />;
      },
    },
    {key: 'turns', header: 'Turns', mono: true, align: 'end', cell: line => line.turns},
    {key: 'cost', header: 'Cost', mono: true, align: 'end', cell: line => line.cost},
    {key: 'budget', header: 'Budget', mono: true, align: 'end', cell: line => line.budget ?? '—'},
  ];
}

function Served(props: {
  readonly host: CostOverviewHost;
  readonly model: HostCostScreenModel;
  readonly words: CostOverviewWords;
  readonly open: (host: string, runId: string) => void;
}): JSX.Element {
  const {host, model} = props;
  if (model.empty) return <Text tone="muted">No turns yet.</Text>;
  return (
    <>
      <Card section="By run">
        <Table
          label={`Cost by run on ${host.label}`}
          columns={runColumns(host.key, props.words, props.open)}
          rows={model.runs}
          rowKey={line => line.key}
          middle
        />
      </Card>
      {model.chart === null ? null : <SpendOverTime chart={model.chart} />}
      <Card section="By agent type">
        <BarRows
          label="Cost by agent type"
          nameHeader="Type"
          barLabel="Share of the largest cost"
          columns={TYPE_COLUMNS}
          rows={model.types}
        />
      </Card>
      <ChartColumns align="start">
        <Card section="By model">
          <BarRows
            label="Cost by model"
            nameHeader="Model"
            barLabel="Share of the largest cost"
            columns={MODEL_COLUMNS}
            rows={model.models}
          />
        </Card>
      </ChartColumns>
    </>
  );
}

/**
 * Cost: what every run on every connected host has cost, as each host served it, in the layout of
 * one run's Cost screen. The headline is the sum of the totals the hosts served (a floor while a host
 * has unpriced turns or has not served a view); below it each host shows its runs by cost, its
 * spend over time and its split by agent type and model. A host that cannot serve a view (an older
 * one) says so inline beside its status. The screen computes none of it.
 */
export function CostOverviewScreen(props: {
  readonly hosts: readonly CostOverviewHost[];
  readonly words: CostOverviewWords;
  readonly open: (host: string, runId: string) => void;
  readonly retry: (host: string) => void;
}): JSX.Element {
  const {hosts, words} = props;
  const served = hosts.flatMap(host => (host.view === undefined ? [] : [host.view]));
  const several = hosts.length > 1;
  const [only] = hosts;
  return (
    <Page
      key="cost"
      width="wide"
      state={pageState(
        hosts.some(
          host =>
            host.view === undefined &&
            (host.status.kind === 'waiting' || host.status.kind === 'connecting'),
        ),
      )}
      title="Cost"
      meta={only === undefined || several ? undefined : <HostStatusChip status={only.status} />}
    >
      {hosts.length === 0 ? null : (
        <SpendCard
          spend={{
            ...totalFigures(served, hosts.length),
            cap: null,
            capShare: null,
          }}
          runOver={false}
        />
      )}
      {hosts.map(host => (
        <Stack key={host.key} gap="space-4">
          {several ? (
            <Row gap="space-3" wrap>
              <Text variant="heading">{host.label}</Text>
              <HostStatusChip status={host.status} />
              {host.view === undefined ? null : (
                <Text
                  variant="small"
                  tone="muted"
                >{`As of ${words.moment(host.view.asOfMs)}`}</Text>
              )}
            </Row>
          ) : null}
          <Problem host={host} words={words} retry={props.retry} />
          {host.view === undefined ? null : (
            <Served
              host={host}
              model={hostCostModel(host.key, host.view, words, host.titles)}
              words={words}
              open={props.open}
            />
          )}
        </Stack>
      ))}
    </Page>
  );
}
