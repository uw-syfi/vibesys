/**
 * What the Run screen shows, as selectors over `RunPageState`: the header's words and actions, the
 * startup timeline, the log streams grouped by owner, and the pause and ended banners. The renderer
 * maps these onto `@vibesys/ui` components and decides nothing itself.
 */
import type {LogOwner, RunEvent, RunOverview, Span} from '@vibesys/contracts';
import type {LogStream, UsageRecord} from '@vibesys/contracts/host-helper';
import {endedLook, type HostRun, problemWord, RUN_GONE_WORDS, type RunEnd} from '@vibesys/hosts';
import {decodeNarrationLine, type LogView, logViewRecords} from '@vibesys/logs';
import type {CoreState} from '@vibesys/run-state';
import {type RunEnding, type RunTrace, traceSpans} from '@vibesys/run-state/trace';
import {type AdoptedCandidate, adoptedCandidate, endReason, unclipped} from './end-words.js';
import {overviewAt, traceAt} from './live.js';
import {type ModelNames, NO_MODEL_NAMES} from './model-names.js';
import {
  commandable,
  type HistoryProgress,
  type MachineBlock,
  type RunPageState,
  runPhase,
  type StageView,
  type Unavailable,
} from './run-page.js';
import {STARTUP_LABELS} from './startup-stages.js';
import {type TokensReading, tokensReading} from './token-words.js';
import {roleAgents} from './usage-view.js';
import {whoWords} from './usage-who.js';

export type Tone = 'neutral' | 'live' | 'ok' | 'warn' | 'err';

/** A button the header shows: enabled, or disabled with the reason. */
export interface Offer {
  readonly shown: boolean;
  readonly disabledReason: string | null;
}

export interface HeaderView {
  readonly title: string;
  readonly subtitle: string;
  readonly status: {readonly label: string; readonly tone: Tone};
  /** What the run's turns have spent so far, whole; null until a turn reports any. */
  readonly spent: TokensReading | null;
  /** What ran each role of the run, from its usage records: `Planner` with `model on harness`. */
  readonly agents: readonly {readonly role: string; readonly ran: readonly string[]}[];
  readonly pause: Offer;
  readonly pauseNow: Offer;
  readonly openWindow: Offer;
  readonly archive: Offer;
}

function projectName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

/** An ended run's words, from its end record or, on an older tombstone, its last phase. */
function endedStatus(phase: HostRun['phase'], end: RunEnd): {label: string; tone: Tone} {
  const look = endedLook({phase, end});
  return look === null ? {label: 'Finished', tone: 'ok'} : {label: look.label, tone: look.tone};
}

const BLOCK_STATUS: Readonly<Record<MachineBlock, string>> = {
  'needs-update': problemWord('needs-update'),
  'sign-in': problemWord('sign-in'),
  offline: problemWord('unavailable'),
  closed: 'Disconnected',
};

/**
 * Why the run cannot be found because its machine will not answer (it needs an update, a sign-in,
 * or cannot be reached), else null while it is only being looked for. Typed from the machine's
 * connection, never from the failure text, which may name a path or a user.
 */
export function machineBlockWords(state: RunPageState): string | null {
  if (state.record.kind !== 'unlisted' || state.record.block === undefined) return null;
  const machine = state.hostLabel;
  switch (state.record.block) {
    case 'needs-update':
      return `VibeSys on ${machine} needs an update`;
    case 'sign-in':
      return `Sign in to ${machine}`;
    case 'offline':
      return `Can’t reach ${machine}`;
    case 'closed':
      return `Disconnected from ${machine}`;
  }
}

function status(state: RunPageState): {label: string; tone: Tone} {
  const phase = runPhase(state);
  switch (phase.kind) {
    case 'unlisted': {
      const block = state.record.kind === 'unlisted' ? state.record.block : undefined;
      if (block !== undefined) return {label: BLOCK_STATUS[block], tone: 'warn'};
      const unplaced = unplacedRun(state);
      return unplaced === null
        ? {label: 'Looking for the run', tone: 'neutral'}
        : {label: unplaced.label, tone: 'warn'};
    }
    case 'historical':
      return {label: 'Saved run', tone: 'neutral'};
    case 'starting':
      return {label: 'Starting', tone: 'live'};
    case 'running':
      return phase.pausing ? {label: 'Pausing', tone: 'warn'} : {label: 'Running', tone: 'live'};
    case 'paused':
      return phase.by === 'quota'
        ? {label: 'Paused · usage limit', tone: 'warn'}
        : {label: 'Paused by you', tone: 'warn'};
    case 'stopping':
      return {label: 'Pausing', tone: 'warn'};
    case 'ended':
      return endedStatus(phase.run.phase, phase.end);
  }
}

/** Why a live pause command cannot be sent now, or undefined when they can. */
function commandReason(state: RunPageState): string | undefined {
  if (commandable(state)) return undefined;
  if (state.link.kind === 'failed') return `Could not connect to the run: ${state.link.message}`;
  return 'Connecting to the run…';
}

function offer(shown: boolean, unavailable?: string): Offer {
  return {shown, disabledReason: unavailable ?? null};
}

export function headerView(state: RunPageState, names: ModelNames = NO_MODEL_NAMES): HeaderView {
  const phase = runPhase(state);
  const {project, task} = state.title;
  const name = projectName(project);
  const unavailable: Unavailable = state.unavailable;
  return {
    title: task === null ? name : `${name} · ${task}`,
    subtitle: `${state.hostLabel} · ${state.identity.runId}`,
    status: {
      ...status(state),
      ...(state.lifecycleWord === undefined ? {} : {label: state.lifecycleWord}),
    },
    // Tokens counted over a prefix of the history are not what the run spent.
    spent:
      historyLoading(state) === null && (!state.progress || state.progress.phase === 'complete')
        ? tokensReading(state.run.core?.tokens.total)
        : null,
    agents: roleAgents(usageRecords(state), names).map(each => ({
      role: each.role,
      ran: each.ran.flatMap(who => whoWords(who) ?? []),
    })),
    pause: offer(
      state.readOnly !== true && phase.kind === 'running' && !phase.pausing,
      commandReason(state),
    ),
    pauseNow: offer(
      state.readOnly !== true &&
        state.record.kind === 'live' &&
        !['ended', 'historical', 'unlisted'].includes(phase.kind),
      state.pauseNow.kind === 'idle'
        ? commandReason(state)
        : state.pauseNow.kind === 'confirming'
          ? 'Confirm or cancel below'
          : 'Pausing',
    ),
    openWindow: offer(true, unavailable['open-window']),
    archive: offer(state.readOnly !== true && phase.kind === 'ended'),
  };
}

/** The banner under the header while Pause now is being confirmed or is under way. */
export type PauseNowBannerView =
  | {readonly kind: 'none'}
  | {readonly kind: 'confirm'; readonly title: string; readonly body: string};

export function pauseNowBannerView(state: RunPageState): PauseNowBannerView {
  return state.pauseNow.kind === 'confirming'
    ? {
        kind: 'confirm',
        title: 'Pause now?',
        body: 'Unfinished work may need repeating and may still incur charges.',
      }
    : {kind: 'none'};
}

const STAGES = [
  {id: 'building_image', label: STARTUP_LABELS.building_image},
  {id: 'starting_container', label: STARTUP_LABELS.starting_container},
] as const;

function streamsOf(state: RunPageState): readonly LogStream[] {
  return state.logs.kind === 'listed' || state.logs.kind === 'listing' ? state.logs.streams : [];
}

function startupStream(streams: readonly LogStream[], stage: string): LogStream | undefined {
  return streams
    .filter(stream => stream.owner['kind'] === 'startup_step' && stream.owner['stage'] === stage)
    .at(-1);
}

function streamState(stream: LogStream | undefined, starting: boolean): StageView['state'] {
  if (stream === undefined) return starting ? 'waiting' : 'done';
  if (stream.outcome === null) return starting ? 'active' : 'done';
  return stream.outcome === 'ended' ? 'done' : 'failed';
}

/** The startup timeline: resolving, then each startup step the run's logs record. */
export function timelineView(state: RunPageState): readonly StageView[] {
  if (state.startupStages !== undefined) return state.startupStages;
  const starting = runPhase(state).kind === 'starting';
  const streams = streamsOf(state);
  const stages: StageView[] = STAGES.map(stage => {
    const stream = startupStream(streams, stage.id);
    return {
      id: stage.id,
      label: stage.label,
      state: streamState(stream, starting),
      streamId: stream?.id ?? null,
    };
  });
  // A later stage that started means the ones before it are done, logged or not (a cached image).
  const lastStarted = stages.reduce(
    (last, stage, index) => (stage.state === 'waiting' ? last : index),
    -1,
  );
  const settled = stages.map((stage, index) =>
    index < lastStarted && stage.state === 'waiting' ? {...stage, state: 'done' as const} : stage,
  );
  const resolving: StageView = {
    id: 'resolving',
    label: 'Resolving the project and task',
    state: starting && lastStarted < 0 ? 'active' : 'done',
    streamId: null,
  };
  return [resolving, ...settled];
}

/** Which streams the screen needs followed now: the startup steps while starting, and the open one. */
export function followedStreams(state: RunPageState): readonly string[] {
  const ids = new Set<string>();
  if (runPhase(state).kind === 'starting') {
    for (const stage of timelineView(state)) if (stage.streamId !== null) ids.add(stage.streamId);
  }
  if (state.followingLogs) {
    const shown = shownStream(state);
    if (shown !== null) ids.add(shown);
  }
  return [...ids];
}

export type LogGroupName = 'Startup' | 'Gates' | 'Tool builds' | 'Run narration' | 'Other';
const GROUP_ORDER: readonly LogGroupName[] = [
  'Startup',
  'Gates',
  'Tool builds',
  'Run narration',
  'Other',
];

interface LogStreamItem {
  readonly id: string;
  readonly label: string;
  readonly live: boolean;
}

export interface LogGroup {
  readonly name: LogGroupName;
  readonly streams: readonly LogStreamItem[];
}

function words(value: unknown): string {
  return typeof value === 'string' ? value.replaceAll('_', ' ') : String(value);
}

function streamGroup(stream: LogStream): LogGroupName {
  switch (stream.owner['kind']) {
    case 'startup_step':
      return 'Startup';
    case 'gate_stage':
      return 'Gates';
    case 'tool_build':
      return 'Tool builds';
    case 'run_narration':
      return 'Run narration';
    default:
      return 'Other';
  }
}

type GateKind = Extract<LogOwner, {kind: 'gate_stage'}>['gate'];

/** A gate stage's name in a stream label; exhaustive, so a new gate kind is a compile error. */
const GATE_WORDS: Readonly<Record<GateKind, string>> = {
  validation: 'validation',
  build: 'candidate build',
  accuracy: 'accuracy',
  benchmark: 'benchmark',
};

function isGateKind(value: unknown): value is GateKind {
  return typeof value === 'string' && Object.hasOwn(GATE_WORDS, value);
}

function gateWords(gate: unknown): string {
  return isGateKind(gate) ? GATE_WORDS[gate] : words(gate);
}

/** The idea each span belongs to, by the span's id: how a stream that names its span names its idea. */
export type IdeaOfSpan = ReadonlyMap<string, string>;

export const NO_IDEAS: IdeaOfSpan = new Map();

/**
 * Whose measurement a gate stage's stream is: the idea that owns the span it names, or the input
 * when it names none (the input's measurement has no decision of its own); nothing when it names
 * a span no idea holds, rather than a guess.
 */
function gateTarget(owner: LogStream['owner'], ideas: IdeaOfSpan): string | null {
  const span = owner['span_id'];
  if (typeof span !== 'string') return 'input';
  return ideas.get(span) ?? null;
}

function streamLabel(stream: LogStream, ideas: IdeaOfSpan): string {
  const {owner} = stream;
  switch (owner['kind']) {
    case 'startup_step':
      return STAGES.find(stage => stage.id === owner['stage'])?.label ?? words(owner['stage']);
    case 'gate_stage':
      return [
        gateWords(owner['gate']),
        gateTarget(owner, ideas),
        `attempt ${String(owner['attempt'])}`,
      ]
        .filter(part => part !== null)
        .join(' \u00b7 ');
    case 'tool_build':
      return `Building ${String(owner['name'])}`;
    case 'run_narration':
      return 'Run narration';
    default:
      return stream.command;
  }
}

/** The run's streams by owner (startup steps, gate stages, tool builds, narration), oldest first. */
export function logGroups(state: RunPageState, ideas: IdeaOfSpan = NO_IDEAS): readonly LogGroup[] {
  const streams = streamsOf(state);
  return GROUP_ORDER.map(name => {
    const items = streams
      .filter(stream => streamGroup(stream) === name)
      .map(stream => ({
        id: stream.id,
        label: streamLabel(stream, ideas),
        live: stream.outcome === null,
      }));
    // A step the run repeated (a restart, a resume) is numbered in order, oldest first.
    const seen = new Map<string, number>();
    const numbered = items.map(item => {
      const total = items.filter(other => other.label === item.label).length;
      const nth = (seen.get(item.label) ?? 0) + 1;
      seen.set(item.label, nth);
      return total === 1 ? item : {...item, label: `${item.label} · ${nth}`};
    });
    return {name, streams: numbered};
  }).filter(group => group.streams.length > 0);
}

/**
 * The stream the Logs tab shows: the selected one while it is listed. Otherwise, once the run has
 * started, its latest narration (what the run says about itself), not the first image build of the
 * startup steps, which the startup timeline already shows; while it starts, or with no narration,
 * the first stream.
 */
export function shownStream(state: RunPageState): string | null {
  const groups = logGroups(state);
  const ids = groups.flatMap(group => group.streams.map(stream => stream.id));
  if (state.selected !== null && ids.includes(state.selected)) return state.selected;
  const narration = groups.find(group => group.name === 'Run narration')?.streams.at(-1);
  if (narration !== undefined && runPhase(state).kind !== 'starting') return narration.id;
  return ids[0] ?? null;
}

export interface LogLineView {
  readonly text: string;
  readonly stream: 'out' | 'err';
}

function narrationText(text: string): string {
  const record = decodeNarrationLine(text);
  if (record.kind === 'raw') return record.text;
  const {at, level, message} = record.line;
  return `${at.slice(11, 19)} ${level} ${message}`;
}

/** A stream's lines for `LogPane`: stderr marked, narration records as one readable line each. */
export function streamLines(state: RunPageState, streamId: string | null): readonly LogLineView[] {
  if (streamId === null) return [];
  const view: LogView | undefined = state.views[streamId];
  if (view === undefined) return [];
  const isNarration =
    streamsOf(state).find(stream => stream.id === streamId)?.owner['kind'] === 'run_narration';
  return logViewRecords(view).map(line => ({
    text: isNarration ? narrationText(line.text) : line.text,
    stream: line.runs.some(run => run.pipe === 'stderr') ? 'err' : 'out',
  }));
}

/** What the Logs tab says instead of lines while the listing is not in or has failed; null once listed. */
export function logsNotice(state: RunPageState): string | null {
  if (state.unavailable.logs !== undefined) return state.unavailable.logs;
  switch (state.logs.kind) {
    case 'failed':
      return state.logs.unsupported
        ? 'This machine’s VibeSys does not serve logs. Update its checkout to read them here.'
        : `Could not list the run’s logs: ${state.logs.message}`;
    case 'idle':
    case 'listing':
      if (state.logs.kind === 'listing' && state.logs.streams.length > 0) return null;
      // A saved run has no server to list logs from, so nothing is looked for: say so, not "looking".
      return state.record.kind === 'historical'
        ? 'This saved run has no running or listed server, so its logs are not available here.'
        : 'Looking for the run’s logs…';
    case 'listed':
      return null;
  }
}

/** The candidate the run adopted and its objective's direction, as the trace at the end says. */
function adoptedAtEnd(
  state: RunPageState,
): [AdoptedCandidate | 'input' | null, 'max' | 'min' | null | undefined] {
  const direction = overviewAt(state.run.trace, null)?.objective.direction;
  return [adoptedCandidate(traceSpans(traceAt(state.run.trace, null))), direction];
}

/**
 * Whether the run's spans have reached the window: `arrived` once a snapshot or a span event is
 * folded; `awaited` while they are on their way (a live run attaching or waiting for its first
 * snapshot, an ended run's history being read: a page of it is a prefix, not the run); `none` when
 * nothing is coming (a run the host does not list, a refused attach, a failed or unsupported
 * history read). A face shows loading while they are awaited, and says it is empty only once they
 * arrived or none will.
 */
export type TraceArrival = 'arrived' | 'awaited' | 'none';

/** An ended run's recorded data, before falling back to the fold already on the page. */
function endedTraceArrival(state: RunPageState): TraceArrival | null {
  if (state.record.kind !== 'ended' && state.record.kind !== 'historical') return null;
  if (state.summary.kind === 'reading') return 'awaited';
  // A ready summary keeps the run level whole while its optional transcript is read or fails.
  switch (state.history.kind) {
    case 'reading':
      return state.summary.kind === 'ready' ? 'arrived' : 'awaited';
    case 'failed':
    case 'unsupported':
      return state.summary.kind === 'ready' ? 'arrived' : 'none';
    case 'idle':
    case 'read':
      return null;
  }
}

export function traceArrival(state: RunPageState): TraceArrival {
  const ended = endedTraceArrival(state);
  if (ended !== null) return ended;
  const {trace} = state.run;
  if (state.session?.hasSnapshot === true || trace.base.sequence > 0 || trace.events.length > 0) {
    return 'arrived';
  }
  switch (state.record.kind) {
    case 'live':
      return state.link.kind === 'attaching' || state.link.kind === 'attached' ? 'awaited' : 'none';
    case 'ended':
    case 'historical':
    case 'unlisted':
      return 'none';
  }
}

/**
 * How far an ended run's history read has come while it is read, else null: the Run screen then
 * shows the progress in place of the run (`HistoryLoading`). The run fold is a prefix of the run
 * until the last page, and no pane draws a prefix as the run: its open spans would read as running
 * and its measurements as the run's only ones.
 */
export function historyLoading(state: RunPageState): HistoryProgress | null {
  if (
    (state.record.kind !== 'ended' && state.record.kind !== 'historical') ||
    state.history.kind !== 'reading'
  )
    return null;
  // A run opened from its summary shows its run level whole while its transcript is read.
  return state.summary.kind === 'ready' ? null : state.history.progress;
}

/**
 * How far the transcript of a run opened from its summary has been read while it is read, else
 * null. This is the fallback for a host without span reads: an opened step waits for the whole
 * transcript, while the run level remains final and usable.
 */
export function transcriptLoading(state: RunPageState): HistoryProgress | null {
  if (
    (state.record.kind !== 'ended' && state.record.kind !== 'historical') ||
    state.history.kind !== 'reading'
  )
    return null;
  return state.summary.kind === 'ready' ? state.history.progress : null;
}

/**
 * Why an ended run's history is not on the screen: the host could not read it, or does not serve
 * it. Null otherwise. The panes then have no run to draw (a failed read drops its prefix), but the
 * run's logs and its end record stay readable.
 */
export function historyProblem(state: RunPageState): string | null {
  if (state.record.kind !== 'ended' && state.record.kind !== 'historical') return null;
  switch (state.history.kind) {
    case 'failed':
      return `Could not read the run’s history: ${state.history.message}`;
    case 'unsupported':
      return 'This machine’s VibeSys does not serve run history. Update its checkout to read finished runs here.';
    default:
      return null;
  }
}

/**
 * Why a past run has nothing to show because its host holds no record of it (the sidebar's words
 * for the same run), else null. Asking again finds nothing, so the screen offers no Retry.
 */
export function runGoneWords(state: RunPageState): string | null {
  if (state.record.kind !== 'ended' && state.record.kind !== 'historical') return null;
  return state.summary.kind === 'gone' ? RUN_GONE_WORDS : null;
}

/**
 * A run the page gave up placing in its host's listing: a status word and the reason in a few
 * words, else null (the page is still waiting, or the run is placed). Asking again finds nothing
 * new until the listing changes, and a later listing settles the page by itself.
 */
export function unplacedRun(
  state: RunPageState,
): {readonly label: string; readonly words: string} | null {
  if (state.record.kind !== 'unlisted' || state.locate.kind !== 'unresolved') return null;
  return state.record.why === 'machine'
    ? {label: 'No answer', words: 'runs not listed'}
    : {label: 'Not confirmed', words: 'live state unknown'};
}

export interface EndedView {
  readonly title: string;
  /** A run that failed is `failed`: the banner holds that state; it is not a passing error. */
  readonly tone: 'neutral' | 'ok' | 'warn' | 'failed';
  readonly reason: string;
  /** "Resume from round N", only when the end record says the run can resume. */
  readonly resume: string | null;
}

/**
 * The run's whole failure message, when the page holds it: the registry's own (`run.reason`), else
 * the terminal event a summary carries. The end record's `reason` is one line cut to a fixed
 * length, so a run opened from its summary (which has no events to read the message from) takes
 * it here.
 */
function wholeReason(state: RunPageState, registry: string | null): string | null {
  if (registry !== null) return registry;
  return state.summary.kind === 'ready' ? (state.summary.terminal?.message ?? null) : null;
}

/** What the ended banner says while the history that settles its verdict is still being read. */
const HISTORY_PENDING = 'Reading the run’s history…';

/** What the page knows of the adopted candidate while the history is not (all) read: nothing. */
const NOT_READ: ReturnType<typeof adoptedAtEnd> = [null, undefined];

/** The ended banner, from the end record. */
export function endedView(state: RunPageState): EndedView | null {
  const phase = runPhase(state);
  if (phase.kind !== 'ended') return null;
  const {label, tone} = endedStatus(phase.run.phase, phase.end);
  const end = phase.end;
  // An adoption is read from the whole trace: a prefix of the history would name the wrong one.
  const loading = historyLoading(state) !== null;
  const [adopted, direction] =
    loading || historyProblem(state) !== null ? NOT_READ : adoptedAtEnd(state);
  const reason =
    end === null
      ? (phase.run.reason ?? 'The run’s record does not say why it ended.')
      : loading && end.outcome === 'adopted'
        ? HISTORY_PENDING
        : endReason(
            end.outcome,
            unclipped(end.reason, wholeReason(state, phase.run.reason)),
            adopted,
            direction,
            end.uncompared === true,
          );
  const resume = 'Resume';
  return {
    title: label,
    tone: tone === 'live' ? 'neutral' : tone === 'err' ? 'failed' : tone,
    reason,
    resume,
  };
}

/**
 * The run as the tabs show it, at the view's sequence: the run fold (`CoreState`, the live end;
 * null before a snapshot), the trace folded at the view's sequence, its spans in start order, and
 * the span events in sequence order (for a tab that folds or scrubs them itself).
 */
export interface ViewedRun {
  readonly core: CoreState | null;
  readonly trace: RunTrace;
  /** The run's declared overview at the view, or null before one was declared. */
  readonly overview: RunOverview | null;
  readonly spans: readonly Span[];
  readonly spanEvents: readonly RunEvent[];
  /** The sequence shown: null at the live end. */
  readonly at: number | null;
}

export function viewedRun(state: RunPageState): ViewedRun {
  const at = state.view.at === 'live' ? null : state.view.at;
  const trace = traceAt(state.run.trace, at);
  return {
    core: state.run.core,
    trace,
    overview: overviewAt(state.run.trace, at),
    spans: traceSpans(trace),
    spanEvents: state.run.trace.events.toArray(),
    at,
  };
}

const NO_RECORDS: readonly UsageRecord[] = [];

/** The span status a run's registry status settles open spans with; null for a status not final. */
function endedSpanStatus(status: HostRun['phase']): RunEnding['status'] | null {
  switch (status) {
    case 'failed':
      return 'failed';
    case 'stopped':
      return 'stopped';
    case 'completed':
      return 'done';
    default:
      return null;
  }
}

/**
 * How the run's end settles the spans it left open, from what the page knows of the run: its
 * registry status and failure, the time the run was last heard, and the turns that recorded their
 * spend. Null while the run is not over, when the registry gives no final status, or while the
 * history is still being read (the fold is then a prefix and no pane draws it).
 */
export function runEnding(state: RunPageState): RunEnding | null {
  if (historyLoading(state) !== null) return null;
  const {record} = state;
  let status: RunEnding['status'] | null = null;
  let reason: string | null = null;
  if (record.kind === 'ended') {
    status = endedSpanStatus(record.run.phase);
    reason = wholeReason(state, record.run.reason);
  } else if (record.kind === 'historical') {
    // A saved run whose tombstone expired may not say how it ended: it was not finished, and the
    // page does not claim it failed.
    status = record.status === null ? 'stopped' : endedSpanStatus(record.status);
  }
  if (status === null) return null;
  return {status, reason, heardAt: state.run.heard?.run ?? null, turns: usageRecords(state)};
}

/**
 * The run's usage records, one per finished turn: the summary's while the run is opened from it
 * (the core it seeds holds spend, not who spent it), else the ones the run fold keeps from the
 * events, which a live session and a history read have. The header's spend is the sum of the same
 * turns.
 */
export function usageRecords(state: RunPageState): readonly UsageRecord[] {
  if (state.summary.kind === 'ready' && state.history.kind !== 'read') return state.summary.records;
  return state.run.core?.usageRecords ?? NO_RECORDS;
}
