import type {AgentRole, RunStatus} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  type Figure,
  FigureStrip,
  MainSide,
  Page,
  pageState,
  Row,
  SegmentedControl,
  Stack,
  StackedBars,
  type StatusTone,
  Swatch,
  SwatchLegend,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type ExploredWorkstreams,
  type FeedStatus,
  HOST_ORDER,
  type HostCostView,
  type HostStatus,
  nextRunSort,
  type RunSort,
  type RunSortKey,
  type SpendRange,
} from '../../state/index.js';
import {AgentTypeTable, type CostCell, FLOOR, formatAxisUsd, NOT_REPORTED} from '../cost/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {
  type BudgetCell,
  type CostOverviewModelWords,
  type HostCostScreenModel,
  hostCostModel,
  type ModelLine,
  NO_VIEW,
  type RunLine,
  totalFigures,
  UNPRICED_FACT_HINT,
} from './cost-overview-model.js';

/** The words of the run's own vocabulary this screen needs beside the cost words. */
export interface CostOverviewWords extends CostOverviewModelWords {
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
  /** What the run/search streams prove about named workstreams on this host. */
  readonly workstreams?: ExploredWorkstreams | undefined;
  /** What the host served for the exact 24-hour window fixed when this page opened. */
  readonly lastDayView?: HostCostView | undefined;
  /** How that separate 24-hour stream stands; absent for callers that only show all-time cost. */
  readonly lastDayFeed?: FeedStatus | undefined;
  /** What each run the client holds a card for is called, by run id; other runs keep the host's name. */
  readonly titles: Readonly<Record<string, string>>;
  /** When each run the client holds a card for began, by run id. */
  readonly started: Readonly<Record<string, number>>;
  /** How many agents of each type are working now, from the runs' summaries the host served. */
  readonly working: ReadonlyMap<AgentRole, number>;
}

function Problem(props: {
  readonly host: CostOverviewHost;
  readonly words: CostOverviewWords;
  readonly retry: (host: string) => void;
}): JSX.Element | null {
  const failures = [
    {window: 'All time', status: props.host.feed},
    ...(props.host.lastDayFeed === undefined
      ? []
      : [{window: 'Last 24 hours', status: props.host.lastDayFeed}]),
  ].flatMap(({window, status}) =>
    status._tag === 'Failed' ? [{window, fault: status.fault}] : [],
  );
  if (failures.length === 0) return null;
  return (
    <Stack gap="space-2">
      {failures.map(({window, fault}) => (
        <Row key={window} gap="space-3" wrap>
          <Chip label={`${window}: ${props.words.fault(fault.kind)}`} tone="warn" dot />
          {fault.retryable ? (
            <Button
              text="Retry"
              label={`Retry ${window.toLowerCase()} cost of ${props.host.label}`}
              variant="secondary"
              size="sm"
              press={() => props.retry(props.host.key)}
            />
          ) : null}
        </Row>
      ))}
    </Stack>
  );
}

/** A cost, whole, with what its turns without a price mean on hover. */
const CostText = (props: {readonly cost: CostCell}): JSX.Element => (
  <Text variant="code" {...(props.cost.hint === undefined ? {} : {title: props.cost.hint})}>
    {props.cost.text}
  </Text>
);

/** A run's budget: its figure, in the problem tone and with the word `over` once the cost is past it; how far along it is, on hover. */
function BudgetText(props: {readonly budget: BudgetCell | null}): JSX.Element | null {
  const {budget} = props;
  if (budget === null)
    return (
      <Text variant="code" tone="muted">
        —
      </Text>
    );
  return (
    <Text variant="code" tone={budget.over ? 'err' : 'muted'} title={budget.hint}>
      {budget.over ? `${budget.text} · over` : budget.text}
    </Text>
  );
}

const SORT_KEYS: readonly RunSortKey[] = ['run', 'turns', 'tokens', 'cost', 'budget'];

function runColumns(words: CostOverviewWords): readonly TableColumn<RunLine>[] {
  return [
    {
      key: 'run',
      header: 'Run',
      sortable: true,
      lead: true,
      clip: line => line.name,
      cell: line => line.name,
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
    {
      key: 'started',
      header: 'Started',
      mono: true,
      wide: true,
      cell: line =>
        line.started === null ? (
          '—'
        ) : (
          <Text variant="code" tone="muted" title={line.started.hint}>
            {line.started.text}
          </Text>
        ),
    },
    {
      key: 'cost',
      header: 'Cost',
      sortable: true,
      mono: true,
      align: 'end',
      cell: line => <CostText cost={line.cost} />,
    },
    {
      key: 'share',
      header: '% of total',
      hint: 'This run’s part of the cost of every run on the host',
      mono: true,
      align: 'end',
      cell: line => line.share,
    },
    {
      key: 'budget',
      header: 'Budget',
      hint: 'The run’s cost limit, and how much of it is spent',
      sortable: true,
      mono: true,
      align: 'end',
      cell: line => <BudgetText budget={line.budget} />,
    },
    {
      key: 'turns',
      header: 'Turns',
      sortable: true,
      mono: true,
      align: 'end',
      cell: line => line.turns,
    },
    {
      key: 'tokens',
      header: 'Input',
      hint: 'Input tokens: fresh input, cache reads and cache writes',
      sortable: true,
      mono: true,
      align: 'end',
      wide: true,
      cell: line => line.tokens.input,
    },
    {
      key: 'cached',
      header: '% cached',
      hint: 'The share of the input tokens that were read from a cache',
      mono: true,
      align: 'end',
      wide: true,
      cell: line => line.tokens.cached,
    },
    {
      key: 'output',
      header: 'Output',
      hint: 'Output tokens',
      mono: true,
      align: 'end',
      wide: true,
      cell: line => line.tokens.output,
    },
  ];
}

const INPUT_HINT = 'Input tokens: fresh input, cache reads and cache writes';

/** A provider/model name, in the code face, after the swatch of its color on the chart. */
function ModelName(props: {readonly line: ModelLine}): JSX.Element {
  const {line} = props;
  return (
    <Row gap="space-2">
      {line.series === null ? <Swatch muted /> : <Swatch series={line.series} />}
      <Text variant="code">{line.name}</Text>
    </Row>
  );
}

const MODEL_COLUMNS: readonly TableColumn<ModelLine>[] = [
  {
    key: 'model',
    header: 'Provider · model',
    lead: true,
    clip: line => line.title,
    cell: line => <ModelName line={line} />,
  },
  {
    key: 'cost',
    header: 'Cost',
    mono: true,
    align: 'end',
    cell: line => <CostText cost={line.cost} />,
  },
  {
    key: 'input',
    header: 'Input',
    hint: INPUT_HINT,
    mono: true,
    align: 'end',
    cell: line => line.tokens.input,
  },
  {
    key: 'cached',
    header: '% cached',
    hint: 'The share of the input tokens that were read from a cache',
    mono: true,
    align: 'end',
    cell: line => line.tokens.cached,
  },
  {
    key: 'output',
    header: 'Output',
    hint: 'Output tokens',
    mono: true,
    align: 'end',
    cell: line => line.tokens.output,
  },
];

/** What a panel with nothing to show says, in one line, by how far along the host is. */
function missingWords(model: HostCostScreenModel, served: string): string {
  if (model.state === 'reading') return 'Reading cost';
  return model.state === 'empty' ? 'No turns yet' : served;
}

/** What the chart says when it has no bar: nothing yet, or nothing priced in the range chosen. */
function chartWords(model: HostCostScreenModel): string {
  if (model.state !== 'served') return missingWords(model, '');
  if (model.range === 'all') return 'No priced turn yet · spend appears as turns report a price';
  return `No spend in the last ${model.range === '1d' ? 'day' : '7 days'}`;
}

function SpendChartCard(props: {
  readonly model: HostCostScreenModel;
  readonly words: CostOverviewWords;
  readonly range: (range: SpendRange) => void;
}): JSX.Element {
  const {model} = props;
  const {chart} = model;
  return (
    <Card
      dense
      section="Spend over time"
      action={
        <Row gap="space-3">
          {model.bucketWords === null ? null : (
            <Text variant="small" tone="muted">
              {model.bucketWords}
            </Text>
          )}
          {model.ranges.length === 0 ? null : (
            <SegmentedControl
              label="Range of the spend chart"
              size="xs"
              options={model.ranges}
              value={model.range}
              change={props.range}
            />
          )}
        </Row>
      }
    >
      {chart === null ? (
        <Text tone="muted" variant="small">
          {chartWords(model)}
        </Text>
      ) : (
        <Stack gap="space-3">
          {chart.legend.length === 0 ? null : <SwatchLegend label="Models" items={chart.legend} />}
          <StackedBars
            label="Spend over time"
            unit="dollars"
            size="compact"
            bars={chart.bars}
            timeAxis={{
              fromMs: chart.domain.fromMs,
              toMs: chart.domain.toMs,
              label: props.words.tick,
              zone: props.words.offset,
            }}
            zeroLabel="$0"
            valueFormat={formatAxisUsd}
          />
        </Stack>
      )}
    </Card>
  );
}

function RunsCard(props: {
  readonly host: CostOverviewHost;
  readonly model: HostCostScreenModel;
  readonly words: CostOverviewWords;
  readonly sort: RunSort;
  readonly pressSort: (key: RunSortKey) => void;
  readonly open: (host: string, runId: string) => void;
}): JSX.Element {
  const {host, model} = props;
  const idOf = new Map(
    model.runs.flatMap(line => (line.runId === null ? [] : [[line.key, line.runId] as const])),
  );
  return (
    <Card dense section="By run">
      <Table
        label={`Cost by run on ${host.label}`}
        dense
        middle
        columns={runColumns(props.words)}
        rows={model.runs}
        rowKey={line => line.key}
        sort={{
          key: props.sort.key,
          direction: props.sort.direction,
          change: key => {
            const pressed = SORT_KEYS.find(each => each === key);
            if (pressed !== undefined) props.pressSort(pressed);
          },
        }}
        select={key => {
          const runId = idOf.get(key);
          if (runId !== undefined) props.open(host.key, runId);
        }}
        empty={missingWords(model, 'No runs')}
      />
    </Card>
  );
}

function SplitCards(props: {
  readonly host: CostOverviewHost;
  readonly model: HostCostScreenModel;
}): JSX.Element {
  const {host, model} = props;
  return (
    <>
      <Card dense section="By agent type">
        <Stack gap="space-3">
          <AgentTypeTable
            label={`Cost by agent type on ${host.label}`}
            rows={model.types}
            empty={missingWords(model, 'No agents')}
          />
          {model.unpricedNote === null ? null : (
            <Text variant="small" tone="muted">
              {model.unpricedNote}
            </Text>
          )}
        </Stack>
      </Card>
      <Card dense section="By CLI provider and model">
        <Stack gap="space-3">
          <Table
            label={`Cost by CLI provider and model on ${host.label}`}
            dense
            middle
            columns={MODEL_COLUMNS}
            rows={model.models}
            rowKey={line => line.key}
            empty={missingWords(model, 'No models')}
          />
          {model.modelsTruncated ? (
            <Text variant="small" tone="muted">
              Some provider and model rows are not shown because the host shortened the list or a
              name. Totals still include every model.
            </Text>
          ) : null}
        </Stack>
      </Card>
    </>
  );
}

/** One host's panels: the chart and its runs in the main column, its splits at the side. */
function HostPanels(props: {
  readonly host: CostOverviewHost;
  readonly words: CostOverviewWords;
  readonly open: (host: string, runId: string) => void;
}): JSX.Element {
  const {host, words} = props;
  const [range, setRange] = useState<SpendRange>('all');
  const [sort, setSort] = useState<RunSort>(HOST_ORDER);
  const model =
    host.view === undefined
      ? NO_VIEW
      : hostCostModel(host.key, host.view, words, {
          titles: host.titles,
          started: host.started,
          working: host.working,
          range,
          sort,
        });
  return (
    <MainSide
      label={`Cost on ${host.label}`}
      main={
        <>
          <SpendChartCard model={model} words={words} range={setRange} />
          <RunsCard
            host={host}
            model={model}
            words={words}
            sort={sort}
            pressSort={key => setSort(nextRunSort(sort, key))}
            open={props.open}
          />
        </>
      }
      side={<SplitCards host={host} model={model} />}
    />
  );
}

const figure = (value: string | null, floor: boolean): string =>
  value === null ? NOT_REPORTED : `${floor ? FLOOR : ''}${value}`;

const countFigure = (value: string, served: number, total: number): string =>
  served === 0 ? NOT_REPORTED : `${served < total ? FLOOR : ''}${value}`;

/** How many machines a figure covers, short enough for a figure's quiet line. */
const covers = (served: number, total: number): string =>
  served === total
    ? `${total} ${total === 1 ? 'machine' : 'machines'}`
    : `${served} of ${total} machines`;

const coverage = (served: number, total: number): string =>
  served === total
    ? total === 1
      ? '1 machine reporting'
      : `${total} machines reporting`
    : `${served} of ${total} machines reporting`;

type Figures = ReturnType<typeof totalFigures>;

/** What the total's quiet line says: the unpriced turns behind a floor, else who reported, else why not. */
function totalDetail(figures: Figures): string {
  const unpriced =
    figures.unpriced === null || figures.unpriced.count === '0'
      ? ''
      : `${figures.unpriced.count} ${figures.unpriced.what}`;
  if (figures.figure !== null)
    return figures.floor && unpriced !== ''
      ? unpriced
      : covers(figures.servedHosts, figures.hostCount);
  if (!figures.complete) return 'Reading cost';
  return figures.unpriced === null ? 'Nothing spent yet' : 'Spend unknown';
}

/** A figure nobody reported reads as a muted dash and never a zero. */
const reported = (is: boolean): {readonly tone?: 'muted'} => (is ? {} : {tone: 'muted'});

function perWorkstreamFigure(figures: Figures): Figure {
  const average = figures.costPerWorkstream;
  return {
    label: 'Per workstream',
    value: average.kind === 'average' ? average.figure : NOT_REPORTED,
    detail: average.kind === 'average' ? 'Spend ÷ workstreams' : 'Not available',
    hint:
      average.kind === 'average'
        ? 'Total priced spend ÷ explored workstreams. An operator-wide average only; individual turns are not attributed to a workstream'
        : average.reason,
    ...reported(average.kind === 'average'),
  };
}

function summaryFigures(figures: Figures): readonly Figure[] {
  const unpriced =
    figures.unpriced === null || figures.unpriced.count === '0'
      ? ''
      : `; ${figures.unpriced.count} ${figures.unpriced.what}`;
  return [
    {
      label: 'Total spend',
      value: figure(figures.figure, figures.floor),
      detail: totalDetail(figures),
      hint: `Priced finished turns across the cost views each machine served; ${coverage(figures.servedHosts, figures.hostCount)}${unpriced}${figures.floor ? `. ${UNPRICED_FACT_HINT}` : ''}`,
      ...reported(figures.figure !== null),
    },
    {
      label: 'Last hour',
      value: figure(figures.lastHour, figures.lastHourFloor),
      detail: figures.lastHourFloor ? 'Partial' : 'Priced turns',
      hint: 'Priced spend from finished turns in the last hour',
      ...reported(figures.lastHour !== null),
    },
    {
      label: 'Last 24 hours',
      value: figure(figures.lastDay, figures.lastDayFloor),
      detail: covers(figures.lastDayHosts, figures.hostCount),
      hint: 'An exact 24-hour view fixed when this Cost page opened',
      ...reported(figures.lastDay !== null),
    },
    {
      label: 'Runs',
      value: countFigure(figures.runs.count, figures.servedHosts, figures.hostCount),
      detail: `${figures.turns.count} finished turns`,
      ...reported(figures.servedHosts > 0),
    },
    {
      label: 'Workstreams',
      value: figure(figures.workstreams, figures.workstreamsFloor),
      detail: covers(figures.workstreamHosts, figures.hostCount),
      hint: 'Workstreams explored: named workstreams with at least one idea, counted once per run from complete search views',
      ...reported(figures.workstreams !== null),
    },
    perWorkstreamFigure(figures),
  ];
}

/**
 * The operator's figures in one band: what has been spent and when, the runs and turns behind it,
 * and what a workstream cost. A figure no machine reported reads as a muted dash and never a zero, a
 * floor says `at least` beside its value, and what a figure covers (how many machines reported) is
 * its quiet line. When the per-workstream average cannot be worked out, the reason is said under the
 * band in words, not left to a hover.
 */
function OperatorSummary(props: {readonly figures: Figures}): JSX.Element {
  const {figures} = props;
  const average = figures.costPerWorkstream;
  return (
    <Stack gap="space-2">
      <FigureStrip label="Cost summary" figures={summaryFigures(figures)} />
      {average.kind === 'average' ? null : (
        <Text variant="small" tone="muted">
          {average.reason}
        </Text>
      )}
    </Stack>
  );
}

/**
 * Cost: what every run on every connected host has cost, as each host served it. The header is the
 * title and one line of facts, led by the host's status; under it a host's panels sit in two columns
 * (the spend over time and the runs by cost at the main side, the split by agent type and by model
 * at the other), every one in its place from the first minute, an empty one saying in a line what it
 * waits for. A host that cannot serve a view (an older one) says so inline beside its status. The
 * screen computes none of it.
 */
export function CostOverviewScreen(props: {
  readonly hosts: readonly CostOverviewHost[];
  readonly words: CostOverviewWords;
  readonly open: (host: string, runId: string) => void;
  readonly retry: (host: string) => void;
  /** The page has not yet fixed the moment its 24-hour window ends. */
  readonly loading?: boolean;
}): JSX.Element {
  const {hosts, words} = props;
  const served = hosts.flatMap(host => (host.view === undefined ? [] : [host.view]));
  const servedLastDay = hosts.flatMap(host =>
    host.lastDayView === undefined ? [] : [host.lastDayView],
  );
  const workstreams = hosts.flatMap(host =>
    host.workstreams === undefined ? [] : [host.workstreams],
  );
  const several = hosts.length > 1;
  const [only] = hosts;
  return (
    <Page
      key="cost"
      width="fill"
      state={pageState(
        props.loading === true ||
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
        <OperatorSummary figures={totalFigures(served, hosts.length, servedLastDay, workstreams)} />
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
          <HostPanels host={host} words={words} open={props.open} />
        </Stack>
      ))}
    </Page>
  );
}
