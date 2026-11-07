import {
  Button,
  CellInline,
  CellLines,
  CellSparkline,
  Chip,
  ColumnList,
  EmptyState,
  type ListColumn,
  Page,
  pageState,
  Row,
  SegmentedControl,
  Stack,
  StatusDot,
  Text,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  FLEET_STATES,
  type FleetFilter,
  type FleetHostInput,
  type FleetHostView,
  type FleetPlace,
  type FleetRow,
  type FleetState,
  fleetView,
  type NavigationAction,
  type ObjectiveReport,
  rowObjectives,
} from '../../state/index.js';
import {
  ago,
  dollars,
  FRESHNESS_WORD,
  freshnessPhrase,
  percent,
  resultHint,
  resultWords,
  seattle,
  span,
} from './fleet-words.js';
import {runWord} from './words.js';

/** A row whose run started, or ended, within this long is new on screen: it arrives with a flash. */
const FRESH_MS = 10_000;
/** The host control's value for every host: no host key is empty. */
const ALL_HOSTS = '';
/** Objectives a row names before it says how many more there are. */
const OBJECTIVE_LINES = 2;

const STATE_WORD: Readonly<Record<FleetState, string>> = {all: 'All', live: 'Live', ended: 'Ended'};

const isFresh = (row: FleetRow, nowMs: number): boolean => {
  const at = row.live ? row.startedMs : row.endedMs;
  return at !== null && nowMs >= at && nowMs - at < FRESH_MS;
};

/** How long the run has gone, or went. */
const elapsed = (row: FleetRow, nowMs: number): string | null =>
  row.startedMs === null ? null : span((row.endedMs ?? nowMs) - row.startedMs);

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

/** What a row says for the status cell's second line: the report's age while live, the end time after. */
function whenLine(row: FleetRow, nowMs: number): string {
  if (!row.live) return row.endedMs === null ? '' : seattle(row.endedMs);
  return row.lastReportMs === null ? 'No report' : `Report ${ago(nowMs - row.lastReportMs)}`;
}

const whenHint = (row: FleetRow, nowMs: number): string => {
  if (!row.live) return row.endedMs === null ? '' : `Ended ${seattle(row.endedMs)}`;
  return row.lastReportMs === null
    ? 'The lead has not posted a report'
    : `Last report ${ago(nowMs - row.lastReportMs)}, ${seattle(row.lastReportMs)}`;
};

/** The columns that name a run and say how it stands, and where it is. */
function identityColumns(nowMs: number): readonly ListColumn<FleetRow>[] {
  return [
    {
      key: 'name',
      header: 'Run',
      size: 'fill',
      hint: row =>
        [
          row.name,
          row.repository,
          row.startedMs === null ? '' : `Started ${seattle(row.startedMs)}`,
        ]
          .filter(line => line !== '')
          .join('\n'),
      cell: row => (
        <CellLines
          main={<Text variant="body-strong">{row.name}</Text>}
          note={
            <Text variant="small" tone="muted">
              {elapsed(row, nowMs)}
              {/* The Host column gives way when the list is narrow; its word moves here. */}
              <span data-narrow-only>{` \u00b7 ${row.host.label}`}</span>
            </Text>
          }
        />
      ),
    },
    {
      key: 'status',
      header: 'Status',
      size: 'wide',

      hint: row => whenHint(row, nowMs) || undefined,
      cell: row => {
        const word = runWord(row.status);
        return (
          <CellLines
            main={<Chip label={word.label} tone={word.tone} dot />}
            note={whenLine(row, nowMs)}
          />
        );
      },
    },
    {
      key: 'host',
      header: 'Host',
      size: 'medium',
      optional: true,
      hint: row => `${row.host.label}\n${freshnessPhrase(row.host.freshness, row.host.staleForMs)}`,
      cell: row => (
        <CellLines
          main={
            <CellInline nowrap>
              <StatusDot
                tone={FRESHNESS_WORD[row.host.freshness].tone}
                label={FRESHNESS_WORD[row.host.freshness].label}
              />
              <Text>{row.host.label}</Text>
            </CellInline>
          }
          note={
            row.host.freshness === 'live'
              ? null
              : freshnessPhrase(row.host.freshness, row.host.staleForMs)
          }
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
      size: 'wide',
      narrow: 'medium',
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
      size: 'compact',
      align: 'end',
      optional: true,
      cell: row => <Text>{row.live ? String(row.agentsRunning) : '—'}</Text>,
    },
    {
      key: 'cost',
      header: 'Cost',
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
  objectivesOf: (row: FleetRow) => readonly ObjectiveReport[],
): readonly ListColumn<FleetRow>[] => [...identityColumns(nowMs), ...figureColumns(objectivesOf)];

const describe = (row: FleetRow, nowMs: number): string =>
  [
    `On ${row.host.label}`,
    runWord(row.status).label,
    resultLines(row).join(', '),
    row.costUsd === null ? undefined : dollars(row.costUsd),
    whenLine(row, nowMs),
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
    return (
      <SegmentedControl
        label="Host"
        value={props.host ?? ALL_HOSTS}
        options={[
          {value: ALL_HOSTS, label: 'All hosts'},
          ...hosts.map(host => ({
            value: host.key,
            label:
              host.freshness === 'live'
                ? host.label
                : `${host.label} · ${freshnessPhrase(host.freshness, host.staleForMs)}`,
          })),
        ]}
        change={host =>
          props.go({type: 'filter-runs-by-host', host: host === ALL_HOSTS ? null : host})
        }
      />
    );
  }
  const [only] = hosts;
  return only === undefined || only.freshness === 'live' ? null : (
    <Chip
      label={`${only.label} · ${freshnessPhrase(only.freshness, only.staleForMs)}`}
      tone={FRESHNESS_WORD[only.freshness].tone}
      dot
    />
  );
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
  /** The way to start a run, kept in the page's header. */
  readonly newRun: ReactNode;
}): JSX.Element {
  const {place, nowMs, go} = props;
  const filter: FleetFilter = {state: place.state, host: place.host};
  const view = fleetView(props.hosts, filter);
  const rows = [...view.live, ...view.history];
  const objectivesOf = rowObjectives(props.hosts);
  const unread = view.hosts.some(
    host => host.freshness === 'empty' || host.freshness === 'unavailable',
  );
  // A host that cannot be read (its link is down, its stream failed) is tried again from here.
  const needRetry = view.hosts.filter(
    host => host.freshness === 'stale' || host.freshness === 'unavailable',
  );
  const open = (key: string): void => {
    const row = rows.find(candidate => candidate.key === key);
    if (row !== undefined) go({type: 'open-run', host: row.host.key, runId: row.runId});
  };
  const list = (label: string, shown: readonly FleetRow[]): JSX.Element => (
    <ColumnList
      label={label}
      columns={columns(nowMs, objectivesOf)}
      rows={shown}
      rowKey={row => row.key}
      lines={2}
      selected={place.selected ?? undefined}
      select={open}
      marks={row => ({fresh: isFresh(row, nowMs)})}
      describe={row => describe(row, nowMs)}
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
          {needRetry.length === 0 ? null : (
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
          )}
        </Row>
        {rows.length === 0 ? (
          // A host that has said nothing yet is unknown, never a claim that it has no runs.
          <EmptyState title={unread ? 'Not read yet' : 'No runs'} />
        ) : (
          <>
            {view.live.length === 0 ? null : (
              <Section title="Live" count={view.live.length}>
                {list('Live runs', view.live)}
              </Section>
            )}
            {view.history.length === 0 ? null : (
              <Section title="History" count={view.history.length}>
                {list('Run history', view.history)}
              </Section>
            )}
          </>
        )}
      </Stack>
    </Page>
  );
}
