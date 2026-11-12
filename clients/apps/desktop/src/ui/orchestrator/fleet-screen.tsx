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
  type StatusTone,
  Text,
  withoutPaths,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  FLEET_STATES,
  type FleetAbsence,
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
} from './fleet-words.js';
import {
  genericDiscoveryIssue,
  genericIndexUnavailableWord,
  genericIndexWords,
  genericRunHint,
  genericRunWord,
} from './generic-run-words.js';
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
  if (row.source === 'generic') return genericRunWord(row).label;
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
  if (row.source === 'generic') return genericIndexWords(row);
  if (!row.live) return row.endedMs === null ? '' : clock.datedMoment(row.endedMs);
  return row.lastReportMs === null ? 'No report' : `Report ${ago(nowMs - row.lastReportMs)}`;
}

/**
 * The Status cell's second line: the most pressing reason to look and how many more there are;
 * else that the host could not serve the report; else `whenLine`.
 */
function statusNote(row: FleetRow, nowMs: number, clock: DisplayClock): ReactNode {
  if (row.source === 'generic') {
    const availability = genericIndexWords(row);
    return (
      <Text
        variant="small"
        tone={row.needsUser || row.indexFreshness === 'stale' ? 'warn' : 'muted'}
      >
        {row.needsUser ? (
          <>
            <span data-wide-only>{`Needs you · ${availability}`}</span>
            <span data-narrow-only>Needs you</span>
          </>
        ) : (
          availability
        )}
      </Text>
    );
  }
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
  if (row.source === 'generic') return genericRunHint(row);
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
            row.source === 'generic' ? (
              <Text variant="code" tone="muted" title={row.runId}>
                {row.runId}
                {/* The Host column gives way when the list is narrow; its words move here. */}
                <span data-narrow-only>{` \u00b7 ${hostWords(row.host)}`}</span>
              </Text>
            ) : (
              <Text variant="small" tone="muted">
                {withoutPaths(row.repository)}
                {/* The Host column gives way when the list is narrow; its words move here. */}
                <span data-narrow-only>{` \u00b7 ${hostWords(row.host)}`}</span>
              </Text>
            )
          }
        />
      ),
    },
    {
      key: 'status',
      header: 'Status',
      sort: sortOf('status'),
      size: 'wide',
      narrow: 'medium',
      hint: row => statusHint(row, nowMs, clock),
      cell: row => (
        <CellLines
          main={
            <Chip
              label={statusLabel(row, nowMs)}
              tone={
                row.source === 'generic'
                  ? genericRunWord(row).tone
                  : runStateWord(row.setup, row.status).tone
              }
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
  if (first === undefined) return <Text>—</Text>;
  const [start] = first.points;
  const end = first.points.at(-1);
  if (start === undefined || end === undefined) return <Text>—</Text>;
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

/** A generic index does not invent a figure that only a run-detail stream could supply. */
const unavailableFigure = (label: 'Not available' | 'Not listed' | 'Not recorded'): ReactNode => (
  <Text variant="small" tone="muted" title={label}>
    <span data-wide-only>{label}</span>
    <span data-narrow-only>—</span>
  </Text>
);

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
      narrow: 'compact',
      cell: row =>
        row.source === 'generic'
          ? unavailableFigure('Not available')
          : trendCell(row, objectivesOf(row)),
    },
    {
      key: 'best',
      header: 'Best',
      narrowHeader: 'Bound %',
      sort: sortOf('best'),
      size: 'wide',
      narrow: 'compact',
      note: {
        label: '% of bound',
        narrowLabel: null,
        hint: 'A percent after a result is how close it is to the bound the agents declared, as the host served it',
      },
      hint: row => row.objectives.map(resultHint).join('\n') || undefined,
      cell: row => {
        if (row.source === 'generic') {
          const [objective] = row.objectives;
          if (objective === undefined || objective.value === null)
            return unavailableFigure('Not recorded');
          return (
            <Text variant="body-strong" title={resultWords(objective)}>
              <span data-wide-only>{resultWords(objective)}</span>
              <span data-narrow-only>—</span>
            </Text>
          );
        }
        const [first, second] = resultLines(row);
        if (first === undefined) return <Text variant="body-strong">—</Text>;
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
      cell: row =>
        row.source === 'generic' ? (
          unavailableFigure('Not listed')
        ) : (
          <Text>{row.live && row.agentsRunning !== null ? String(row.agentsRunning) : '—'}</Text>
        ),
    },
    {
      key: 'cost',
      header: 'Cost',
      sort: sortOf('cost'),
      size: 'short',
      narrow: 'short',
      align: 'end',
      hint: row =>
        row.costUsd === null
          ? row.source === 'generic'
            ? 'Cost was not recorded for this run'
            : 'No cost reported'
          : `${row.costUsd.toFixed(2)} dollars${row.budgetUsd === null ? '' : ` of ${row.budgetUsd.toFixed(2)}`}`,
      cell: row => {
        if (row.costUsd === null)
          return row.source === 'generic' ? unavailableFigure('Not recorded') : <Text>—</Text>;
        return (
          <CellLines
            main={<Text>{dollars(row.costUsd)}</Text>}
            note={row.budgetUsd === null ? null : `of ${dollars(row.budgetUsd)}`}
          />
        );
      },
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

interface DiscoveryIssue {
  readonly label: string;
  readonly tone: StatusTone;
  readonly hint?: string;
}

/** The degraded discovery sources of one host; healthy sources make no claim for failed ones. */
function discoveryIssues(host: FleetHostView): readonly DiscoveryIssue[] {
  const found: DiscoveryIssue[] = [];
  const managed = host.discovery.managed.status;
  if (managed.kind !== 'live') {
    const word = hostStatusWord(managed);
    found.push({
      label: `Managed details · ${word.label}`,
      tone: word.tone,
      ...(word.hint === undefined ? {} : {hint: word.hint}),
    });
  }
  const generic = genericDiscoveryIssue(host.discovery.generic);
  if (generic !== null) found.push(generic);
  return found;
}

/** A filter option names each discovery source that is degraded, never the host as a whole. */
const fleetHostWords = (host: FleetHostView): string => {
  const issues = discoveryIssues(host);
  return issues.length === 0
    ? host.label
    : `${host.label} · ${issues.map(issue => issue.label).join(' · ')}`;
};

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
            ...hosts.map(host => ({value: host.key, label: fleetHostWords(host)})),
          ]}
          change={host =>
            props.go({type: 'filter-runs-by-host', host: host === ALL_HOSTS ? null : host})
          }
        />
      </Field>
    );
  }
  const [only] = hosts;
  if (only === undefined) return null;
  const issues = discoveryIssues(only);
  return issues.length === 0 ? null : (
    <Row gap="space-1">
      {issues.map(issue => (
        <Chip
          key={issue.label}
          label={issue.label}
          tone={issue.tone}
          {...(issue.hint === undefined ? {} : {hint: issue.hint})}
          dot
          clip
        />
      ))}
    </Row>
  );
}

/** The widest managed-detail limitation: the room reserved while there is none. */
const WIDEST_LEFT_OUT = '999 older runs lack managed details';

/**
 * How many older runs the managed stream omitted. The generic index may still restore their rows,
 * so the words promise only that their managed details are absent, never that the rows are hidden.
 */
function RunsLeftOut(props: {
  readonly hosts: readonly FleetHostView[];
  readonly host: string | null;
}): JSX.Element {
  const left = props.hosts
    .filter(host => props.host === null || host.key === props.host)
    .reduce((sum, host) => sum + host.runsLeftOut, 0);
  return left === 0 ? (
    <Reserve collapseInNarrowWindow>
      <Chip label={WIDEST_LEFT_OUT} tone="warn" dot />
    </Reserve>
  ) : (
    <Chip label={`${left} older runs lack managed details`} tone="warn" dot />
  );
}

function emptyWords(
  state: FleetState,
  absence: FleetAbsence,
): {readonly title: string; readonly body?: string} {
  switch (absence.kind) {
    case 'complete':
      return {title: emptyTitle(state)};
    case 'opening':
      return {
        title: 'Not read yet',
        body: 'Waiting for every selected host to provide complete run indexes.',
      };
    case 'unconfirmed':
      return {
        title: 'Run count unconfirmed',
        body: 'Some run data is stale or cannot yet be classified for this filter.',
      };
    case 'unavailable':
      return {
        title: 'Run index unavailable',
        body:
          absence.reason === null
            ? 'One or more selected hosts could not provide a complete run index.'
            : genericIndexUnavailableWord(absence.reason),
      };
  }
}

/** The way to try the hosts that cannot be read again; its room is kept while none needs it. */
function RetryHosts(props: {
  readonly hosts: readonly FleetHostView[];
  readonly retryHost: (host: string) => void;
}): JSX.Element {
  const needRetry = props.hosts.filter(
    host =>
      host.discovery.managed.freshness === 'stale' ||
      host.discovery.managed.freshness === 'unavailable',
  );
  const button = (
    <Button
      text="Retry managed"
      icon="refresh"
      variant="quiet"
      size="sm"
      hint={`Retry managed run details for ${needRetry.map(host => host.label).join(', ')}`}
      press={() => {
        for (const host of needRetry) props.retryHost(host.key);
      }}
    />
  );
  return needRetry.length === 0 ? <Reserve collapseInNarrowWindow>{button}</Reserve> : button;
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
  const unknown = sortedRows(view.unknown, place.sort);
  const rows = [...live, ...history, ...unknown];
  const sortOf: SortOf = column => ({
    direction: place.sort?.column === column ? place.sort.direction : null,
    press: () => go({type: 'sort-runs', column}),
  });
  const objectivesOf = rowObjectives(props.hosts);
  const empty = emptyWords(place.state, view.absence);
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
            title={empty.title}
            {...(empty.body === undefined ? {} : {body: empty.body})}
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
            {unknown.length === 0 ? null : (
              <Section title="Status unavailable" count={unknown.length}>
                {list('Runs with unavailable status', unknown)}
              </Section>
            )}
          </>
        )}
      </Stack>
    </Page>
  );
}
