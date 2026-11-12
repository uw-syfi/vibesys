import type {AgentRole, RunStatus} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  CountedFact,
  FactLine,
  KeyValue,
  MainSide,
  MetricTile,
  MetricTiles,
  MutedFact,
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
import {
  AgentTypeTable,
  type CostCell,
  FLOOR,
  FLOOR_HINT,
  floored,
  formatAxisUsd,
  NOT_REPORTED,
} from '../cost/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {
  type BudgetCell,
  type CostOverviewModelWords,
  type HostCostScreenModel,
  hostCostModel,
  type ModelLine,
  NO_VIEW,
  type RunLine,
  type TotalFigures,
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

const coverage = (served: number, total: number): string =>
  served === total
    ? total === 1
      ? '1 machine reporting'
      : `${total} machines reporting`
    : `${served} of ${total} machines reporting`;

/** The eight operator figures requested by the Cost destination, with absences kept explicit. */
function OperatorSummary(props: {readonly figures: ReturnType<typeof totalFigures>}): JSX.Element {
  const {figures} = props;
  const allCoverage = coverage(figures.servedHosts, figures.hostCount);
  const dayCoverage = coverage(figures.lastDayHosts, figures.hostCount);
  const perWorkstream = figures.costPerWorkstream;
  return (
    <MetricTiles label="Cost summary" count={8} dense>
      <MetricTile
        name="Total spend"
        figure={figure(figures.figure, figures.floor)}
        {...(figures.floor ? {note: 'at least'} : {})}
        {...(figures.figure === null ? {tone: 'muted' as const} : {})}
        detail={allCoverage}
        hint="Priced finished turns across the cost views each machine served"
      />
      <MetricTile
        name="Last hour"
        figure={figure(figures.lastHour, figures.lastHourFloor)}
        {...(figures.lastHourFloor ? {note: 'partial'} : {})}
        {...(figures.lastHour === null ? {tone: 'muted' as const} : {})}
        detail="Priced spend from finished turns in the last hour"
      />
      <MetricTile
        name="Last 24 hours"
        figure={figure(figures.lastDay, figures.lastDayFloor)}
        {...(figures.lastDayFloor ? {note: 'at least'} : {})}
        {...(figures.lastDay === null ? {tone: 'muted' as const} : {})}
        detail={dayCoverage}
        hint="An exact 24-hour view fixed when this Cost page opened"
      />
      <MetricTile
        name="Runs"
        figure={countFigure(figures.runs.count, figures.servedHosts, figures.hostCount)}
        {...(figures.servedHosts === 0 ? {tone: 'muted' as const} : {})}
        detail={allCoverage}
      />
      <MetricTile
        name="Turns"
        figure={countFigure(figures.turns.count, figures.servedHosts, figures.hostCount)}
        {...(figures.servedHosts === 0 ? {tone: 'muted' as const} : {})}
        detail="Finished turns"
      />
      <MetricTile
        name="Unpriced turns"
        figure={countFigure(figures.unpriced?.count ?? '0', figures.servedHosts, figures.hostCount)}
        {...(figures.servedHosts === 0 ? {tone: 'muted' as const} : {})}
        detail="Tokens reported without a price"
      />
      <MetricTile
        name="Workstreams explored"
        figure={figure(figures.workstreams, figures.workstreamsFloor)}
        {...(figures.workstreamsFloor ? {note: 'at least'} : {})}
        {...(figures.workstreams === null ? {tone: 'muted' as const} : {})}
        detail={coverage(figures.workstreamHosts, figures.hostCount)}
        hint="Named workstreams with at least one idea, counted once per run from complete search views"
      />
      <MetricTile
        name="Cost per workstream"
        figure={perWorkstream.kind === 'average' ? perWorkstream.figure : NOT_REPORTED}
        {...(perWorkstream.kind === 'unavailable' ? {tone: 'muted' as const} : {})}
        detail={
          perWorkstream.kind === 'average'
            ? 'Total priced spend ÷ explored workstreams'
            : perWorkstream.reason
        }
        hint="Operator-wide average only; individual turns are not attributed to a workstream"
      />
    </MetricTiles>
  );
}

/** The controls the current authoritative contracts can and cannot enforce. */
function CostControls(): JSX.Element {
  return (
    <Card section="Cost controls and attribution">
      <KeyValue
        items={[
          {
            key: 'Run cost limits',
            value:
              'Enforced by each machine. Select a run below, then choose Adjust to review or change its cost limit.',
          },
          {
            key: 'Provider limits',
            value:
              'Not available yet. Machines attribute provider cost after turns finish and expose no provider-specific cost quota.',
          },
          {
            key: 'Provider and model',
            value:
              'The rows below use what finished turns actually reported, never a model merely allowed for a run.',
          },
          {
            key: 'Workstreams',
            value:
              'Complete search views provide the explored count. The headline is an operator-wide average, not turn attribution.',
          },
        ]}
      />
    </Card>
  );
}

/**
 * The page's facts in one line, led by the host's status when there is one host: what has been
 * spent (a floor while turns are unpriced), the last hour's spend when a host served it, the runs and
 * turns counted, and in the quiet voice the turns left without a price, with what that means on hover.
 */
function Facts(props: {
  readonly totals: TotalFigures;
  readonly status: HostStatus | undefined;
}): JSX.Element {
  const {totals} = props;
  const marked = (figure: string, floor: boolean): string => (floor ? floored(figure) : figure);
  return (
    <FactLine spaced>
      {props.status === undefined ? null : <HostStatusChip status={props.status} />}
      {totals.figure === null ? (
        <MutedFact
          text={
            !totals.complete
              ? 'reading cost'
              : totals.unpriced === null
                ? 'nothing spent yet'
                : 'spend unknown'
          }
        />
      ) : (
        <CountedFact
          figure={marked(totals.figure, totals.floor)}
          words="spent"
          {...(totals.floor ? {hint: FLOOR_HINT} : {})}
        />
      )}
      {totals.lastHour === null ? null : (
        <CountedFact
          figure={marked(totals.lastHour, totals.lastHourFloor)}
          words="in the last hour"
          {...(totals.lastHourFloor ? {hint: FLOOR_HINT} : {})}
        />
      )}
      <CountedFact figure={totals.runs.count} words={totals.runs.what} />
      <CountedFact figure={totals.turns.count} words={totals.turns.what} />
      {totals.unpriced === null ? null : (
        <MutedFact
          text={`${totals.unpriced.count} ${totals.unpriced.what}`}
          hint={UNPRICED_FACT_HINT}
        />
      )}
    </FactLine>
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
      meta={
        hosts.length === 0 ? undefined : (
          <Facts
            totals={totalFigures(served, hosts.length)}
            status={only === undefined || several ? undefined : only.status}
          />
        )
      }
    >
      {hosts.length === 0 ? null : (
        <>
          <OperatorSummary
            figures={totalFigures(served, hosts.length, servedLastDay, workstreams)}
          />
          <CostControls />
        </>
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
