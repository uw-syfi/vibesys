import {RunStatus} from '@vibesys/orchestrator-proto';
import {
  Button,
  Chip,
  Dialog,
  EmptyState,
  Field,
  KeyValue,
  type MenuItem,
  Page,
  Row,
  SearchBox,
  SegmentedControl,
  Select,
  Sparkline,
  Stack,
  type StatusTone,
  SummaryCard,
  type SummaryFlag,
  type SummaryResult,
  type SummaryStat,
  Text,
  withoutPaths,
} from '@vibesys/ui';
import {type JSX, type ReactNode, useState} from 'react';
import type {RunSearchState, RunsRefreshState} from '../../core/index.js';
import {
  FLEET_STATES,
  type FleetAbsence,
  type FleetFilter,
  type FleetHostInput,
  type FleetHostRef,
  type FleetHostView,
  type FleetPlace,
  type FleetRemoteSearch,
  type FleetRow,
  type FleetState,
  fleetOverview,
  fleetView,
  type ManagedFleetRow,
  type NavigationAction,
  type ObjectiveReport,
  rowObjectives,
  runIsUnread,
} from '../../state/index.js';
import {useDisplayClock} from '../display-clock.js';
import type {DisplayClock} from '../display-time.js';
import {hostStatusWord} from '../host-status-words.js';
import {FleetOverviewBar} from './fleet-overview.js';
import {
  ago,
  attentionWords,
  dollars,
  percent,
  resultHint,
  resultWords,
  runTitle,
  span,
} from './fleet-words.js';
import {
  genericDiscoveryIssue,
  genericIndexUnavailableWord,
  genericIndexWords,
  genericRunHint,
  genericRunTitle,
  genericRunWord,
} from './generic-run-words.js';
import {runStateWord, setupTitle} from './setup/index.js';

/** The host control's value for every host: no host key is empty. */
const ALL_HOSTS = '';

const STATE_WORD: Readonly<Record<FleetState, string>> = {
  all: 'All',
  live: 'Live',
  ended: 'Ended',
};

/** What a row is called: its task's title, or, in setup with no task, the state and repository. */
const rowTitle = (row: FleetRow): string =>
  row.source === 'generic'
    ? genericRunTitle(row)
    : row.setup
      ? setupTitle(row.repository)
      : runTitle(row.card, row.name);

/** A host's name, followed by its status only when it is not current. */
const hostWords = (host: FleetHostRef): string =>
  host.status.kind === 'live' ? host.label : `${host.label} · ${hostStatusWord(host.status).label}`;

/** A run still starting or running says for how long. */
function statusLabel(row: FleetRow, nowMs: number): string {
  if (row.source === 'generic') return genericRunWord(row).label;
  const {label} = runStateWord(row.setup, row.status);
  const counting = row.status === RunStatus.STARTING || row.status === RunStatus.RUNNING;
  return counting && row.startedMs !== null ? `${label} ${span(nowMs - row.startedMs)}` : label;
}

function whenLine(row: FleetRow, nowMs: number, clock: DisplayClock): string {
  if (row.source === 'generic') return genericIndexWords(row);
  if (!row.live) return row.endedMs === null ? '' : clock.datedMoment(row.endedMs);
  return row.lastReportMs === null ? 'No report yet' : `Report ${ago(nowMs - row.lastReportMs)}`;
}

/** The full operational account behind the short status chip. */
function statusHint(row: FleetRow, nowMs: number, clock: DisplayClock): string {
  if (row.source === 'generic') return genericRunHint(row);
  const {label} = runStateWord(row.setup, row.status);
  const since =
    row.endedMs !== null
      ? `${label} ${clock.datedMoment(row.endedMs)}${
          row.startedMs === null ? '' : `, after ${span(row.endedMs - row.startedMs)}`
        }`
      : row.startedMs === null
        ? label
        : `${label}, started ${clock.datedMoment(row.startedMs)}`;
  return [
    since,
    ...row.attention.map(attention => {
      const words = attentionWords(attention, nowMs, clock);
      return `${words.label}: ${words.hint}`;
    }),
    row.reportUnavailable ? 'Report unavailable: the host could not serve it' : '',
    row.deadlineMs === null ? '' : `Time limit ${clock.datedMoment(row.deadlineMs)}`,
  ]
    .filter(Boolean)
    .join('\n');
}

/** The first two best results, compact enough for a card footer. */
function resultsWords(row: FleetRow): string {
  const results = row.objectives.slice(0, 2).map(objective => {
    const result = resultWords(objective);
    return objective.percentOfBound === null
      ? result
      : `${result} (${percent(objective.percentOfBound)} of bound)`;
  });
  const more = row.objectives.length - results.length;
  return results.length === 0 ? '' : `${results.join(', ')}${more > 0 ? ` +${more}` : ''}`;
}

/** The newest structured work item, never guessed from narration. */
function latestOf(row: FleetRow): {readonly label: string; readonly text: string} | null {
  const update = row.update ?? ({kind: 'none'} as const);
  if (update.kind === 'none') return null;
  const labels = {
    hypothesis: 'Latest hypothesis',
    workstream: 'Latest workstream',
    report: 'Latest report',
  } as const;
  return {
    label: labels[update.kind],
    text: update.detail.trim() === '' ? update.title : `${update.title} — ${update.detail}`,
  };
}

/** A measured objective expands to fill the card's right side. */
function trend(row: FleetRow, measured: readonly ObjectiveReport[]): ReactNode {
  if (row.source === 'generic') {
    return (
      <Stack gap="space-1" align="center" justify="center">
        <Text variant="small" tone="muted">
          The host's run index has a headline result, not the measurement series.
        </Text>
      </Stack>
    );
  }
  const [first] = measured;
  const start = first?.points[0];
  const end = first?.points.at(-1);
  if (first === undefined || start === undefined || end === undefined) {
    const title =
      row.searchOnly === true ? 'Measurements not in search result' : 'Awaiting measurements';
    const detail =
      row.searchOnly === true
        ? 'Whole-history search includes the latest summary, not the measurement series.'
        : 'The graph will fill as candidates are evaluated.';
    return (
      <Stack gap="space-1" align="center" justify="center">
        <Text variant="section">{title}</Text>
        <Text variant="small" tone="muted">
          {detail}
        </Text>
      </Stack>
    );
  }
  return (
    <Sparkline
      label={`${first.name}: every measured candidate and the best so far${
        measured.length > 1 ? `; ${measured.length - 1} more objectives` : ''
      }`}
      points={first.points.map(point => ({x: point.atMs, y: point.value}))}
      steps={first.bestPath.map(point => ({x: point.atMs, y: point.value}))}
      from={start.atMs}
      to={end.atMs}
      reference={start.value}
      tone={row.live ? 'accent' : 'muted'}
    />
  );
}

function metadata(row: FleetRow, nowMs: number, clock: DisplayClock): string {
  const results = resultsWords(row);
  if (row.source === 'generic') {
    return [
      hostWords(row.host),
      row.projectRoot === null ? 'Project unavailable' : withoutPaths(row.projectRoot),
      row.runId,
      results,
      row.costUsd === null ? '' : dollars(row.costUsd),
      `${row.needsUser ? 'Needs you · ' : ''}${genericIndexWords(row)}`,
    ]
      .filter(Boolean)
      .join(' · ');
  }
  return [hostWords(row.host), withoutPaths(row.repository), whenLine(row, nowMs, clock)]
    .filter(Boolean)
    .join(' · ');
}

interface DiscoveryIssue {
  readonly label: string;
  readonly tone: StatusTone;
  readonly hint?: string;
}

/** Degraded discovery sources are named separately; neither may speak for the other. */
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
  if (props.hosts.length > 1) {
    return (
      <Field label="Host" hideLabel>
        <Select
          labelInside
          capped
          size="sm"
          value={props.host ?? ALL_HOSTS}
          options={[
            {value: ALL_HOSTS, label: 'All hosts'},
            ...props.hosts.map(host => ({value: host.key, label: fleetHostWords(host)})),
          ]}
          change={host =>
            props.go({type: 'filter-runs-by-host', host: host === ALL_HOSTS ? null : host})
          }
        />
      </Field>
    );
  }
  const [only] = props.hosts;
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

function leftOut(hosts: readonly FleetHostView[], host: string | null): number {
  return hosts
    .filter(candidate => host === null || candidate.key === host)
    .reduce((sum, candidate) => sum + candidate.runsLeftOut, 0);
}

interface SearchIndicator {
  readonly label: string;
  readonly tone: 'live' | 'ok' | 'warn';
  readonly hint?: string;
}

/** One honest account of searches needed because the snapshot omitted old runs. */
function searchIndicator(
  hosts: readonly FleetHostView[],
  host: string | null,
  query: string,
  states: Readonly<Record<string, RunSearchState>>,
): SearchIndicator | null {
  if (query.trim() === '') return null;
  const targets = hosts.filter(
    candidate => candidate.runsLeftOut > 0 && (host === null || candidate.key === host),
  );
  if (targets.length === 0) return null;
  const current = targets.map(target => states[target.key] ?? ({_tag: 'Idle'} as const));
  if (current.some(state => state._tag === 'Searching'))
    return {label: 'Searching all run history…', tone: 'live'};
  const failed = current.filter(state => state._tag === 'Failed');
  if (failed.length > 0)
    return {
      label:
        failed.length === targets.length
          ? 'Whole-history search unavailable'
          : 'Some history unavailable',
      tone: 'warn',
      hint: failed
        .map(state => (state._tag === 'Failed' ? state.fault.detail : ''))
        .filter(Boolean)
        .join('\n'),
    };
  const invalid = current.find(state => state._tag === 'Invalid');
  if (invalid?._tag === 'Invalid')
    return {label: 'Search query is too long', tone: 'warn', hint: invalid.reason};
  const found = current.filter(state => state._tag === 'Found');
  if (found.some(state => state._tag === 'Found' && state.response.truncated))
    return {
      label: 'More matching runs exist',
      tone: 'warn',
      hint: 'The host reached its bounded search response. Narrow the query to see other matches.',
    };
  return found.length === targets.length
    ? {label: 'All run records searched', tone: 'ok'}
    : {label: 'Older runs not searched yet', tone: 'warn'};
}

function RefreshControl(props: {
  readonly hosts: readonly FleetHostView[];
  readonly host: string | null;
  readonly states: Readonly<Record<string, RunsRefreshState>>;
  readonly refresh: (host: string) => void;
}): JSX.Element {
  const targets = props.hosts.filter(host => props.host === null || host.key === props.host);
  const states = targets.map(host => props.states[host.key] ?? ({_tag: 'Idle'} as const));
  const refreshing = states.some(state => state._tag === 'Refreshing');
  const failed = states.some(state => state._tag === 'Failed');
  const refreshed = states.some(state => state._tag === 'Refreshed');
  const indicator = refreshing
    ? {label: 'Refreshing…', tone: 'live' as const}
    : failed
      ? {label: 'Refresh failed', tone: 'err' as const}
      : refreshed
        ? {label: 'Refreshed', tone: 'ok' as const}
        : null;
  return (
    <Row gap="space-2" align="center">
      <Button
        text="Refresh"
        icon="refresh"
        variant="quiet"
        size="sm"
        hint="Read a fresh authoritative managed-runs snapshot"
        press={() => {
          for (const host of targets) props.refresh(host.key);
        }}
        {...(targets.length === 0
          ? {disabledReason: 'Connect a machine before refreshing', quietReason: true}
          : refreshing
            ? {disabledReason: 'A refresh is already in progress', quietReason: true}
            : {})}
      />
      {indicator === null ? null : <Chip label={indicator.label} tone={indicator.tone} dot />}
    </Row>
  );
}

function Section(props: {
  readonly title: string;
  readonly rows: readonly FleetRow[];
  readonly card: (row: FleetRow) => ReactNode;
}): JSX.Element {
  return (
    <section aria-label={`${props.title} runs`}>
      <Stack gap="space-3">
        <Row gap="space-2" align="baseline">
          <Text as="h2" variant="heading">
            {props.title}
          </Text>
          <Text tone="muted">{String(props.rows.length)}</Text>
        </Row>
        {props.rows.map(props.card)}
      </Stack>
    </section>
  );
}

function emptyCopy(
  hosts: readonly FleetHostView[],
  filter: FleetFilter,
  absence: FleetAbsence,
): {readonly title: string; readonly body: string} {
  if (hosts.length === 0) {
    return {
      title: 'No machines connected',
      body: 'Add a machine or server, or start a run after choosing one.',
    };
  }
  if ((filter.query ?? '').trim() !== '') {
    return {
      title: absence.kind === 'complete' ? 'No matching runs' : 'No matching indexed runs',
      body:
        'Try a title, description, task model, CLI, objective, workstream, repository, or run ID.' +
        (absence.kind === 'complete' ? '' : ' Some run sources are not current.'),
    };
  }
  switch (absence.kind) {
    case 'opening':
      return {title: 'Waiting for runs', body: 'Waiting for each selected run source to answer.'};
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
    case 'complete':
      break;
  }
  if (filter.state !== 'all')
    return {
      title: `No ${STATE_WORD[filter.state].toLocaleLowerCase()} runs`,
      body: 'Change the filter to see other runs.',
    };
  return {title: 'No runs yet', body: 'Start a run to see its live progress here.'};
}

interface FleetScreenProps {
  readonly hosts: readonly FleetHostInput[];
  readonly place: FleetPlace;
  /** Milliseconds since the epoch; the caller owns the clock. */
  readonly nowMs: number;
  readonly go: (action: NavigationAction) => void;
  readonly refreshStates: Readonly<Record<string, RunsRefreshState>>;
  /** Replace one host's displayed runs from an authoritative snapshot. */
  readonly refreshHost: (host: string) => void;
  readonly searchStates: Readonly<Record<string, RunSearchState>>;
  /** The way to start a run, always kept in the page header. */
  readonly newRun: ReactNode;
}

/** How far along its cost limit a run is, 0 to 1; null for a run with no limit or no cost yet. */
const budgetShare = (row: FleetRow): number | null =>
  row.budgetUsd === null || row.budgetUsd <= 0 || row.costUsd === null
    ? null
    : row.costUsd / row.budgetUsd;

const NOT_REPORTED = '\u2014';

/** A slot the host has not filled: a muted dash that says why on hover, never a zero. */
const notReported = (label: string, hint: string): SummaryStat => ({
  label,
  value: NOT_REPORTED,
  tone: 'muted',
  hint,
});

function spendStat(row: FleetRow): SummaryStat {
  if (row.costUsd === null) return notReported('Spend', 'No finished turn reported a cost');
  const share = budgetShare(row);
  return {
    label: 'Spend',
    value: dollars(row.costUsd),
    detail: row.budgetUsd === null ? 'No cost limit' : `of ${dollars(row.budgetUsd)}`,
    ...(share === null ? {} : {share, tone: share >= 0.9 ? ('warn' as const) : ('ok' as const)}),
    hint:
      row.budgetUsd === null
        ? 'What finished turns have cost so far'
        : `What finished turns have cost so far, against the run's ${dollars(row.budgetUsd)} limit`,
  };
}

function workstreamsStat(row: ManagedFleetRow): SummaryStat {
  if (row.workstreams == null) return notReported('Workstreams', 'Not read from the host yet');
  const ideas = row.ideas ?? 0;
  return {
    label: 'Workstreams',
    value: String(row.workstreams),
    ...(ideas === 0 ? {} : {detail: `${ideas} ${ideas === 1 ? 'idea' : 'ideas'} tried`}),
    hint: 'Named lines of work this run started',
  };
}

function agentsStat(row: ManagedFleetRow): SummaryStat {
  if (row.agentsRunning === null)
    return notReported('Agents', 'The host has not sent a summary yet');
  return {
    label: 'Agents',
    value: `${row.agentsRunning} working`,
    ...(row.fanoutLimit == null ? {} : {detail: `of ${row.fanoutLimit + 1} allowed`}),
    hint: 'Agents starting, in a turn or waiting on a subagent, as the host counts them; the lead plus up to its fan-out limit of subagents',
  };
}

function timeLeftStat(row: ManagedFleetRow, nowMs: number): SummaryStat {
  if (row.deadlineMs === null) return {label: 'Time left', value: 'No limit', tone: 'muted'};
  const left = row.deadlineMs - nowMs;
  return left > 0
    ? {label: 'Time left', value: span(left), detail: 'until its limit'}
    : {label: 'Time left', value: 'None', detail: 'limit reached', tone: 'warn'};
}

/**
 * The figures that tell runs apart, each a fact the host served. A live run always has the same four
 * slots in the same order (spend, agents, workstreams, time left), so figures line up down the list;
 * a slot the host has not filled says so with a dash and never a zero. An ended run drops the two
 * that only a live run has.
 */
function cardStats(row: FleetRow, nowMs: number): readonly SummaryStat[] {
  if (row.source !== 'managed') return row.costUsd === null ? [] : [spendStat(row)];
  if (row.live)
    return [spendStat(row), agentsStat(row), workstreamsStat(row), timeLeftStat(row, nowMs)];
  const ranFor =
    row.startedMs !== null && row.endedMs !== null
      ? [{label: 'Ran for', value: span(row.endedMs - row.startedMs)}]
      : [];
  return [spendStat(row), workstreamsStat(row), ...ranFor];
}

/** Why a run wants a look, one chip each, in the host's order of urgency. */
function cardFlags(row: FleetRow, nowMs: number, clock: DisplayClock): readonly SummaryFlag[] {
  if (row.source === 'generic') {
    return [
      ...(row.needsUser ? [{label: 'Needs you', tone: 'warn' as const}] : []),
      ...(row.indexFreshness === 'stale'
        ? [{label: 'Index out of date', tone: 'warn' as const}]
        : []),
    ];
  }
  return [
    ...row.attention.map(attention => {
      const words = attentionWords(attention, nowMs, clock);
      return {label: words.label, tone: words.tone, hint: words.hint};
    }),
    ...(row.reportUnavailable
      ? [{label: 'Report unavailable', tone: 'warn' as const, hint: 'The host could not serve it'}]
      : []),
  ];
}

/** The best result so far, led by its share of the bound when the host worked one out. */
function cardResult(row: FleetRow): SummaryResult | null {
  const [best] = row.objectives;
  if (best === undefined || best.value === null) return null;
  const more = row.objectives.length - 1;
  const parts = [
    best.percentOfBound === null ? '' : `${percent(best.percentOfBound)} of bound`,
    more > 0 ? `+${more} more` : '',
  ].filter(Boolean);
  return {
    label: 'Best so far',
    value: resultWords(best),
    ...(parts.length === 0 ? {} : {detail: parts.join(' · ')}),
    hint: row.objectives.map(resultHint).join('\n'),
  };
}

function cardDescription(row: FleetRow): string | null {
  if (row.source === 'generic')
    return row.projectRoot === null
      ? 'Generic run discovered by the host run index.'
      : `Generic run in ${withoutPaths(row.projectRoot)}.`;
  const description = (row.description ?? '').trim();
  return description === '' ? null : description;
}

const activityKeyOf = (row: FleetRow): string => row.activityKey ?? `${row.source}:${row.runId}`;

function cardLatest(row: FleetRow): {readonly label: string; readonly text: string} | null {
  if (row.source !== 'generic') return latestOf(row);
  return {
    label: row.needsUser ? 'Operator action' : 'Run index',
    text: row.needsUser
      ? 'This run is waiting for a response in the client or workflow that started it.'
      : genericIndexWords(row),
  };
}

function cardTone(row: FleetRow, unread: boolean, nowMs: number, clock: DisplayClock): StatusTone {
  if (unread) return 'warn';
  if (row.source === 'generic')
    return row.needsUser || row.indexFreshness === 'stale' ? 'warn' : genericRunWord(row).tone;
  const needsAttention =
    row.attention.some(attention => attentionWords(attention, nowMs, clock).tone === 'warn') ||
    row.reportUnavailable;
  return needsAttention ? 'warn' : runStateWord(row.setup, row.status).tone;
}

function cardMenu(
  row: FleetRow,
  unread: boolean,
  go: (action: NavigationAction) => void,
  open: (row: FleetRow) => void,
  showDetails: (row: FleetRow) => void,
): readonly MenuItem[] {
  const receipt: MenuItem = unread
    ? {
        id: 'read',
        label: 'Mark as read',
        press: () =>
          go({
            type: 'mark-run-read',
            host: row.host.key,
            runId: row.runId,
            activityKey: activityKeyOf(row),
          }),
      }
    : {
        id: 'unread',
        label: 'Mark as unread',
        press: () => go({type: 'mark-run-unread', host: row.host.key, runId: row.runId}),
      };
  return [
    {id: 'open', label: 'Open run', press: () => open(row)},
    {id: 'details', label: 'Open details and description', press: () => showDetails(row)},
    receipt,
  ];
}

function RunCard(props: {
  readonly row: FleetRow;
  readonly unread: boolean;
  readonly nowMs: number;
  readonly clock: DisplayClock;
  readonly objectives: readonly ObjectiveReport[];
  readonly go: (action: NavigationAction) => void;
  readonly open: (row: FleetRow) => void;
  readonly showDetails: (row: FleetRow) => void;
}): JSX.Element {
  const {row, unread, nowMs, clock, go} = props;
  return (
    <div
      role="option"
      tabIndex={0}
      aria-description={statusHint(row, nowMs, clock)}
      onClick={event => {
        if (event.target === event.currentTarget) props.open(row);
      }}
      onKeyDown={event => {
        if (event.target === event.currentTarget && (event.key === 'Enter' || event.key === ' '))
          props.open(row);
      }}
    >
      <SummaryCard
        title={rowTitle(row)}
        description={cardDescription(row)}
        latest={cardLatest(row)}
        chart={trend(row, props.objectives)}
        metadata={metadata(row, nowMs, clock)}
        stats={cardStats(row, nowMs)}
        flags={cardFlags(row, nowMs, clock)}
        result={cardResult(row)}
        status={{tone: cardTone(row, unread, nowMs, clock), label: statusLabel(row, nowMs)}}
        attention={unread || (row.source === 'generic' && row.needsUser)}
        label={`Open run ${rowTitle(row)}`}
        hint={statusHint(row, nowMs, clock)}
        menu={cardMenu(row, unread, go, props.open, props.showDetails)}
        press={() => props.open(row)}
      />
    </div>
  );
}

interface DetailItem {
  readonly key: string;
  readonly value: string;
  readonly mono?: boolean;
}

function detailItems(row: FleetRow, nowMs: number): readonly DetailItem[] {
  const common: DetailItem[] = [
    {key: 'Status', value: statusLabel(row, nowMs)},
    {key: 'Machine', value: hostWords(row.host)},
    {
      key: row.source === 'generic' ? 'Project' : 'Repository',
      value: row.repository || 'Unavailable',
      mono: row.repository !== '',
    },
    {key: 'Run ID', value: row.runId, mono: true},
  ];
  if (row.source === 'generic') {
    return [
      ...common,
      {key: 'Index', value: genericIndexWords(row)},
      {key: 'Needs you', value: row.needsUser ? 'Yes' : 'No'},
      ...(row.objectives.length === 0
        ? []
        : [{key: 'Objectives', value: row.objectives.map(resultHint).join(' · ')}]),
    ];
  }
  return [
    ...common,
    ...(row.card?.modelName === undefined || row.card.modelName === ''
      ? []
      : [{key: 'Task model', value: row.card.modelName}]),
    ...(row.update === undefined || row.update.kind === 'none'
      ? []
      : [{key: `Latest ${row.update.kind}`, value: latestOf(row)?.text ?? row.update.title}]),
    ...(row.objectives.length === 0
      ? []
      : [{key: 'Objectives', value: row.objectives.map(resultHint).join(' · ')}]),
  ];
}

function detailsDescription(row: FleetRow): string {
  return row.source === 'generic'
    ? 'This run is listed by the host. Detailed activity is not available in this app version.'
    : row.description || `Optimization run in ${withoutPaths(row.repository)}.`;
}

function RunDetailsDialog(props: {
  readonly row: FleetRow | null;
  readonly nowMs: number;
  readonly dismiss: () => void;
  readonly open: (row: FleetRow) => void;
}): JSX.Element {
  const {row} = props;
  return (
    <Dialog
      open={row !== null}
      title={row === null ? 'Run details' : rowTitle(row)}
      dismiss={props.dismiss}
      actions={
        row === null ? null : (
          <>
            <Button text="Close" variant="quiet" press={props.dismiss} />
            <Button
              text="Open run"
              variant="primary"
              press={() => {
                props.dismiss();
                props.open(row);
              }}
            />
          </>
        )
      }
    >
      {row === null ? null : (
        <Stack gap="space-4">
          <Text>{detailsDescription(row)}</Text>
          <KeyValue items={detailItems(row, props.nowMs)} />
        </Stack>
      )}
    </Dialog>
  );
}

/**
 * Every run across connected hosts as large, full-width operator cards. Search, filters, receipts,
 * and the selected row live in navigation so they survive opening a run and coming back.
 */
export function FleetScreen(props: FleetScreenProps): JSX.Element {
  const {place, nowMs, go} = props;
  const query = place.query ?? '';
  const clock = useDisplayClock();
  const [details, setDetails] = useState<FleetRow | null>(null);
  const filter: FleetFilter = {state: place.state, host: place.host, query};
  const remote: FleetRemoteSearch[] = Object.entries(props.searchStates).flatMap(([host, state]) =>
    state._tag === 'Found' ? [{host, query: state.query, results: state.response.results}] : [],
  );
  const view = fleetView(props.hosts, filter, remote);
  const rows = [...view.live, ...view.history, ...view.unknown];
  const overview = fleetOverview(rows);
  const objectivesOf = rowObjectives(props.hosts);
  const open = (row: FleetRow): void =>
    go({
      type: 'open-run',
      host: row.host.key,
      runId: row.runId,
      activityKey: activityKeyOf(row),
    });
  const card = (row: FleetRow): ReactNode => (
    <RunCard
      key={row.key}
      row={row}
      unread={runIsUnread(place.receipts[row.key], activityKeyOf(row))}
      nowMs={nowMs}
      clock={clock}
      objectives={objectivesOf(row)}
      go={go}
      open={open}
      showDetails={setDetails}
    />
  );
  const omitted = leftOut(view.hosts, place.host);
  const searchedAll = searchIndicator(view.hosts, place.host, query, props.searchStates);
  const empty = emptyCopy(view.hosts, filter, view.absence);
  return (
    <>
      <Page
        key="runs"
        width="wide"
        state="ready"
        header={
          <Row justify="between" gap="space-4" align="center" wrap>
            <Text as="h1" variant="title">
              Runs
            </Text>
            <SearchBox
              label="Search runs"
              value={query}
              placeholder="Search title, model, workstream, description…"
              change={query => go({type: 'filter-runs-by-query', query})}
            />
            {props.newRun}
          </Row>
        }
      >
        <Stack gap="space-5">
          <Row gap="space-4" align="center" justify="between" wrap>
            <Row gap="space-3" align="center" wrap>
              <SegmentedControl
                label="Show"
                value={place.state}
                options={FLEET_STATES.map(state => ({value: state, label: STATE_WORD[state]}))}
                change={state => go({type: 'filter-runs-by-state', state})}
              />
              <HostFilter hosts={view.hosts} host={place.host} go={go} />
              {omitted === 0 ? null : searchedAll === null ? (
                <Chip label={`${omitted} older runs lack managed details`} tone="warn" dot />
              ) : (
                <Chip
                  label={searchedAll.label}
                  tone={searchedAll.tone}
                  {...(searchedAll.hint === undefined ? {} : {hint: searchedAll.hint})}
                  dot
                />
              )}
            </Row>
            <RefreshControl
              hosts={view.hosts}
              host={place.host}
              states={props.refreshStates}
              refresh={props.refreshHost}
            />
          </Row>
          <FleetOverviewBar overview={overview} />
          {rows.length === 0 ? (
            <EmptyState title={empty.title} body={empty.body} align="start" />
          ) : (
            <>
              {view.live.length === 0 ? null : (
                <Section title="Live" rows={view.live} card={card} />
              )}
              {view.history.length === 0 ? null : (
                <Section title="Ended" rows={view.history} card={card} />
              )}
              {view.unknown.length === 0 ? null : (
                <Section title="Status unavailable" rows={view.unknown} card={card} />
              )}
            </>
          )}
        </Stack>
      </Page>
      <RunDetailsDialog row={details} nowMs={nowMs} dismiss={() => setDetails(null)} open={open} />
    </>
  );
}
