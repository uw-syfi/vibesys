import {RunStatus} from '@vibesys/orchestrator-proto';
import {
  Button,
  CellInline,
  CellLines,
  CellSparkline,
  Chip,
  ColumnList,
  EmptyState,
  Field,
  type ListColumn,
  MiddleText,
  Page,
  pageState,
  Reserve,
  Row,
  SegmentedControl,
  Select,
  Stack,
  StatusDot,
  Text,
  withoutPaths,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  FLEET_STATES,
  type FleetColumn,
  type FleetFilter,
  type FleetHostInput,
  type FleetHostRef,
  type FleetHostView,
  type FleetPlace,
  type FleetRow,
  type FleetState,
  fleetView,
  type NavigationAction,
  type ObjectiveReport,
  rowObjectives,
} from '../../state/index.js';
import {useDisplayClock} from '../display-clock.js';
import type {DisplayClock} from '../display-time.js';
import {HostStatusChip} from '../host-status-chip.js';
import {hostStatusWord} from '../host-status-words.js';
import {sortedRows} from './fleet-sort.js';
import {rowTitle, rowTitleParts} from './fleet-title.js';
import {
  ago,
  attentionWords,
  dollars,
  percent,
  resultHint,
  resultWords,
  span,
  unreadTitle,
} from './fleet-words.js';
import {runStateWord} from './setup/index.js';

/** A row whose run started, or ended, within this long is new on screen: it arrives with a flash. */
const FRESH_MS = 10_000;
/** The host control's value for every host: no host key is empty. */
const ALL_HOSTS = '';
/** Objectives a row names before it says how many more there are. */
const OBJECTIVE_LINES = 2;

const STATE_WORD: Readonly<Record<FleetState, string>> = {all: 'All', live: 'Live', ended: 'Ended'};

/** What an empty list says, by the state the person chose: the filter is in the words. */
const EMPTY_TITLE: Readonly<Record<FleetState, string>> = {
  all: 'No runs',
  live: 'No live runs',
  ended: 'No ended runs',
};
const emptyTitle = (state: FleetState): string => EMPTY_TITLE[state];

const isFresh = (row: FleetRow, nowMs: number): boolean => {
  const at = row.live ? row.startedMs : row.endedMs;
  return at !== null && nowMs >= at && nowMs - at < FRESH_MS;
};

/** A run still starting or running says for how long in its chip: `Running 5h 20m`. */
function statusLabel(row: FleetRow, nowMs: number): string {
  const {label} = runStateWord(row.setup, row.status);
  const counting = row.status === RunStatus.STARTING || row.status === RunStatus.RUNNING;
  return counting && row.startedMs !== null ? `${label} ${span(nowMs - row.startedMs)}` : label;
}

/** A run's objectives as the lines of the Best cell: the first two, and how many more. */
function resultLines(row: FleetRow): readonly string[] {
  if (row.objectives.length === 0) return ['—'];
  const lines = row.objectives.slice(0, OBJECTIVE_LINES).map(objective => {
    const result = resultWords(objective);
    return objective.percentOfBound === null
      ? result
      : `${result} · ${percent(objective.percentOfBound)}`;
  });
  const more = row.objectives.length - OBJECTIVE_LINES;
  return more > 0 ? [...lines.slice(0, -1), `${lines.at(-1)} +${more}`] : lines;
}

/** What a row says when nothing wants a look: the report's age while live, the end time after. */
function whenLine(row: FleetRow, nowMs: number, clock: DisplayClock): string {
  if (!row.live) return row.endedMs === null ? '' : clock.datedMoment(row.endedMs);
  return row.lastReportMs === null ? 'No report' : `Report ${ago(nowMs - row.lastReportMs)}`;
}

/**
 * The Status cell's second line: the most pressing reason to look and how many more there are;
 * else that the host could not serve the report; else `whenLine`.
 */
function statusNote(row: FleetRow, nowMs: number, clock: DisplayClock): ReactNode {
  const [first, ...others] = row.attention;
  if (first === undefined) {
    return row.reportUnavailable ? (
      <Chip label="Report unavailable" tone="warn" />
    ) : (
      whenLine(row, nowMs, clock)
    );
  }
  const words = attentionWords(first, nowMs, clock);
  return (
    <Text variant="small" tone={words.tone}>
      {others.length === 0 ? words.label : `${words.label} +${others.length}`}
    </Text>
  );
}

/** The report a live row was last summed up by, for the pointer. */
function reportHint(row: FleetRow, nowMs: number, clock: DisplayClock): string {
  if (!row.live) return '';
  if (row.reportUnavailable) return 'Report unavailable: the host could not serve it';
  return row.lastReportMs === null
    ? 'The lead has not posted a report'
    : `Last report ${ago(nowMs - row.lastReportMs)}, ${clock.datedMoment(row.lastReportMs)}`;
}

/** The Status cell whole, for the pointer: how long, every reason to look, the report and the limit. */
function statusHint(row: FleetRow, nowMs: number, clock: DisplayClock): string {
  const {label} = runStateWord(row.setup, row.status);
  const since =
    row.endedMs !== null
      ? `${label} ${clock.datedMoment(row.endedMs)}${row.startedMs === null ? '' : `, after ${span(row.endedMs - row.startedMs)}`}`
      : row.startedMs === null
        ? label
        : `${label}, started ${clock.datedMoment(row.startedMs)}`;
  return [
    since,
    ...row.attention.map(attention => {
      const words = attentionWords(attention, nowMs, clock);
      return `${words.label}: ${words.hint}`;
    }),
    reportHint(row, nowMs, clock),
    row.deadlineMs === null ? '' : `Time limit ${clock.datedMoment(row.deadlineMs)}`,
  ]
    .filter(line => line !== '')
    .join('\n');
}

/** A host's name, and after it the host's one status when it is not live: ` · Stale 14m`. */
const hostWords = (host: FleetHostRef): string =>
  host.status.kind === 'live' ? host.label : `${host.label} · ${hostStatusWord(host.status).label}`;

/** How a column's header sorts: its direction now and the press that sorts by it; none if it cannot. */
type SortOf = (column: FleetColumn) => ListColumn<FleetRow>['sort'];

/** The columns that name a run and say how it stands, and where it is. */
function identityColumns(
  nowMs: number,
  clock: DisplayClock,
  sortOf: SortOf,
): readonly ListColumn<FleetRow>[] {
  return [
    {
      key: 'name',
      header: 'Run',
      sort: sortOf('name'),
      size: 'fill',
      hint: row => [rowTitle(row), row.repository, `On ${hostWords(row.host)}`].join('\n'),
      cell: row => (
        <CellLines
          main={<Text variant="body-strong">{rowTitleParts(row).subject}</Text>}
          mainTail={
            rowTitleParts(row).hardware === '' ? null : (
              <Text variant="body-strong">{` \u00b7 ${rowTitleParts(row).hardware}`}</Text>
            )
          }
          note={
            <Text variant="small" tone="muted">
              {withoutPaths(row.repository)}
              {/* The Host column gives way when the list is narrow; its words move here. */}
              <span data-narrow-only>{` \u00b7 ${hostWords(row.host)}`}</span>
            </Text>
          }
        />
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sort: sortOf('status'),
      size: 'wide',
      narrow: 'short',
      hint: row => statusHint(row, nowMs, clock),
      cell: row => (
        <CellLines
          main={
            <Chip
              label={statusLabel(row, nowMs)}
              tone={runStateWord(row.setup, row.status).tone}
              dot
              // The label grows with a live run's running time; cut it instead of spilling into
              // Best when the column is narrow, rather than leaving the row's own wider hint.
              clip
            />
          }
          note={statusNote(row, nowMs, clock)}
        />
      ),
    },
    {
      key: 'host',
      header: 'Host',
      sort: sortOf('host'),
      size: 'wide',
      optional: true,
      hint: row => `${row.host.label}\n${hostStatusWord(row.host.status).label}`,
      cell: row => (
        <CellLines
          main={
            <CellInline nowrap>
              <StatusDot
                tone={hostStatusWord(row.host.status).tone}
                label={hostStatusWord(row.host.status).label}
              />
              <Text>
                <MiddleText text={row.host.label} />
              </Text>
            </CellInline>
          }
          note={row.host.status.kind === 'live' ? null : hostStatusWord(row.host.status).label}
        />
      ),
    },
  ];
}

/**
 * The first measured objective as a small trend: every candidate as a faint dot and the points the
 * host marked as setting a best as the line over them, with how many more objectives were measured.
 * A run with no series (nothing measured yet, or a host that serves none) has an empty cell.
 */
function trendCell(row: FleetRow, measured: readonly ObjectiveReport[]): ReactNode {
  const [first] = measured;
  if (first === undefined) return null;
  const [start] = first.points;
  const end = first.points.at(-1);
  if (start === undefined || end === undefined) return null;
  return (
    <CellSparkline
      label={`${first.name}: every measured candidate and the best so far`}
      points={first.points.map(point => ({x: point.atMs, y: point.value}))}
      steps={first.bestPath.map(point => ({x: point.atMs, y: point.value}))}
      from={start.atMs}
      to={end.atMs}
      tone={row.live ? 'accent' : 'muted'}
      more={measured.length - 1}
    />
  );
}

/** The columns of figures: the measured history, the best results, the agents working and the cost. */
function figureColumns(
  objectivesOf: (row: FleetRow) => readonly ObjectiveReport[],
  sortOf: SortOf,
): readonly ListColumn<FleetRow>[] {
  return [
    {
      key: 'trend',
      header: 'Candidates',
      size: 'wide',
      optional: true,
      cell: row => trendCell(row, objectivesOf(row)),
    },
    {
      key: 'best',
      header: 'Best',
      sort: sortOf('best'),
      size: 'wide',
      narrow: 'short',
      note: {
        label: '% of bound',
        hint: 'A percent after a result is how close it is to the bound the agents declared, as the host served it',
      },
      hint: row => row.objectives.map(resultHint).join('\n') || undefined,
      cell: row => {
        const [first = '—', second] = resultLines(row);
        return (
          <CellLines main={<Text variant="body-strong">{first}</Text>} note={second ?? null} />
        );
      },
    },
    {
      key: 'agents',
      header: 'Agents',
      sort: sortOf('agents'),
      size: 'compact',
      align: 'end',
      optional: true,
      cell: row => (
        <Text>{row.live && row.agentsRunning !== null ? String(row.agentsRunning) : '—'}</Text>
      ),
    },
    {
      key: 'cost',
      header: 'Cost',
      sort: sortOf('cost'),
      size: 'short',
      narrow: 'compact',
      align: 'end',
      hint: row =>
        row.costUsd === null
          ? 'No cost reported'
          : `${row.costUsd.toFixed(2)} dollars${row.budgetUsd === null ? '' : ` of ${row.budgetUsd.toFixed(2)}`}`,
      cell: row => (
        <CellLines
          main={<Text>{row.costUsd === null ? '—' : dollars(row.costUsd)}</Text>}
          note={row.budgetUsd === null ? null : `of ${dollars(row.budgetUsd)}`}
        />
      ),
    },
  ];
}

const columns = (
  nowMs: number,
  clock: DisplayClock,
  objectivesOf: (row: FleetRow) => readonly ObjectiveReport[],
  sortOf: SortOf,
): readonly ListColumn<FleetRow>[] => [
  ...identityColumns(nowMs, clock, sortOf),
  ...figureColumns(objectivesOf, sortOf),
];

const describe = (row: FleetRow, nowMs: number, clock: DisplayClock): string =>
  [
    `On ${hostWords(row.host)}`,
    statusLabel(row, nowMs),
    ...row.attention.map(attention => attentionWords(attention, nowMs, clock).label),
    resultLines(row).join(', '),
    row.costUsd === null ? undefined : dollars(row.costUsd),
    whenLine(row, nowMs, clock),
  ]
    .filter(part => part !== undefined && part !== '')
    .join('. ');

function Section(props: {
  readonly title: string;
  readonly count: number;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <Stack gap="space-2">
      <Row gap="space-2">
        <Text as="h2" variant="heading">
          {props.title}
        </Text>
        <Text tone="muted">{String(props.count)}</Text>
      </Row>
      {props.children}
    </Stack>
  );
}

function HostFilter(props: {
  readonly hosts: readonly FleetHostView[];
  readonly host: string | null;
  readonly go: (action: NavigationAction) => void;
}): JSX.Element | null {
  const {hosts} = props;
  if (hosts.length > 1) {
    // One control however many hosts there are: a row of buttons for each host would take a row
    // of its own in a narrow window.
    return (
      <Field label="Host" hideLabel>
        <Select
          labelInside
          capped
          size="sm"
          value={props.host ?? ALL_HOSTS}
          options={[
            {value: ALL_HOSTS, label: 'All hosts'},
            ...hosts.map(host => ({value: host.key, label: hostWords(host)})),
          ]}
          change={host =>
            props.go({type: 'filter-runs-by-host', host: host === ALL_HOSTS ? null : host})
          }
        />
      </Field>
    );
  }
  const [only] = hosts;
  return only === undefined || only.status.kind === 'live' ? null : (
    <HostStatusChip status={only.status} clip />
  );
}

/** The widest the count of older runs reads in its chip: the room reserved while there is none. */
const WIDEST_LEFT_OUT = '999 older runs not shown';

/**
 * How many runs the hosts shown did not list: a status beside the filters. The host says it
 * (`runs_left_out`); the screen never counts the rows it was given. While every run is listed its
 * room is kept empty, so the chip that arrives with a late host never moves the rows under it.
 */
function RunsLeftOut(props: {
  readonly hosts: readonly FleetHostView[];
  readonly host: string | null;
}): JSX.Element {
  const left = props.hosts
    .filter(host => props.host === null || host.key === props.host)
    .reduce((sum, host) => sum + host.runsLeftOut, 0);
  return left === 0 ? (
    <Reserve>
      <Chip label={WIDEST_LEFT_OUT} tone="warn" dot />
    </Reserve>
  ) : (
    <Chip label={`${left} older runs not shown`} tone="warn" dot />
  );
}

/** The way to try the hosts that cannot be read again; its room is kept while none needs it. */
function RetryHosts(props: {
  readonly hosts: readonly FleetHostView[];
  readonly retryHost: (host: string) => void;
}): JSX.Element {
  const needRetry = props.hosts.filter(
    host => host.freshness === 'stale' || host.freshness === 'unavailable',
  );
  const button = (
    <Button
      text="Retry"
      icon="refresh"
      variant="quiet"
      size="sm"
      hint={`Try ${needRetry.map(host => host.label).join(', ')} again`}
      press={() => {
        for (const host of needRetry) props.retryHost(host.key);
      }}
    />
  );
  return needRetry.length === 0 ? <Reserve>{button}</Reserve> : button;
}

/**
 * Every run of every connected host: the live ones, then the history, each row a summary that opens
 * the run. The filters and the selected row are the navigation's (`place`), so a run opened and left
 * finds the list as it was. The rows scroll inside their lists and only the ones in view are drawn,
 * so a host's whole history costs a screenful.
 */
export function FleetScreen(props: {
  readonly hosts: readonly FleetHostInput[];
  readonly place: FleetPlace;
  /** Milliseconds since the epoch; the caller owns the clock. */
  readonly nowMs: number;
  readonly go: (action: NavigationAction) => void;
  /** The person asked to try a host again. */
  readonly retryHost: (host: string) => void;
  /** The way to start a run: in the page's header in every state, even with no runs. */
  readonly newRun: ReactNode;
}): JSX.Element {
  const {place, nowMs, go} = props;
  const clock = useDisplayClock();
  const filter: FleetFilter = {state: place.state, host: place.host};
  const view = fleetView(props.hosts, filter);
  const live = sortedRows(view.live, place.sort);
  const history = sortedRows(view.history, place.sort);
  const rows = [...live, ...history];
  const sortOf: SortOf = column => ({
    direction: place.sort?.column === column ? place.sort.direction : null,
    press: () => go({type: 'sort-runs', column}),
  });
  const objectivesOf = rowObjectives(props.hosts);
  const unread = view.hosts.some(
    host => host.freshness === 'empty' || host.freshness === 'unavailable',
  );
  const open = (key: string): void => {
    const row = rows.find(candidate => candidate.key === key);
    if (row !== undefined) go({type: 'open-run', host: row.host.key, runId: row.runId});
  };
  const list = (label: string, shown: readonly FleetRow[]): JSX.Element => (
    <ColumnList
      label={label}
      columns={columns(nowMs, clock, objectivesOf, sortOf)}
      rows={shown}
      rowKey={row => row.key}
      lines={2}
      widths={place.widths}
      resize={(column, width) => go({type: 'resize-runs-column', column, width})}
      selected={place.selected ?? undefined}
      select={open}
      marks={row => ({fresh: isFresh(row, nowMs)})}
      describe={row => describe(row, nowMs, clock)}
    />
  );
  return (
    <Page
      key="runs"
      width="wide"
      state={pageState(props.hosts.length === 0)}
      header={
        <Row justify="between" wrap>
          <Text as="h1" variant="title">
            Runs
          </Text>
          {props.newRun}
        </Row>
      }
    >
      <Stack gap="space-5">
        <Row gap="space-4" wrap>
          <SegmentedControl
            label="Show"
            value={place.state}
            options={FLEET_STATES.map(state => ({value: state, label: STATE_WORD[state]}))}
            change={state => go({type: 'filter-runs-by-state', state})}
          />
          <HostFilter hosts={view.hosts} host={place.host} go={go} />
          <RunsLeftOut hosts={view.hosts} host={place.host} />
          <RetryHosts hosts={view.hosts} retryHost={props.retryHost} />
        </Row>
        {rows.length === 0 ? (
          // A host that has said nothing yet is unknown, never a claim that it has no runs.
          // Text only: New run is in the header in every state, so it never moves with the filter.
          <EmptyState
            title={unread ? unreadTitle(view.hosts) : emptyTitle(place.state)}
            align="start"
          />
        ) : (
          <>
            {live.length === 0 ? null : (
              <Section title="Live" count={live.length}>
                {list('Live runs', live)}
              </Section>
            )}
            {history.length === 0 ? null : (
              <Section title="History" count={history.length}>
                {list('Run history', history)}
              </Section>
            )}
          </>
        )}
      </Stack>
    </Page>
  );
}
