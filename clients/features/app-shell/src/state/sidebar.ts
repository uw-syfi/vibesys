/**
 * The sidebar's store: a pure reducer over what the hosts' watches push (their listings), the runs
 * this user opened (Recent), the runs archived, and which screen is shown. The rows the sidebar
 * draws are selectors over it, so a push is folded once and every row follows. The saved machines
 * are the Machines store's, not kept here.
 */
import {
  connectionProblem,
  digestLook,
  type EndKind,
  type EndLook,
  endedLook,
  type HostKey,
  type HostListing,
  type HostRun,
  hostFromKey,
  hostLabel,
  type ListedRun,
  type MachineProblem,
  problemWord,
  RUN_GONE_WORDS,
  type RunDigestEntry,
  type RunDigestsReply,
} from '@vibesys/hosts';
import {type RecentRun, type RunRef, type SkippedRecent, sameRun} from '@vibesys/runs';
import type {Step} from '@vibesys/store';
import {
  ALL_RUNS_EMPTY,
  type AllRunsState,
  askProjectRuns,
  type ListProjectRuns,
  listedRunRefs,
  projectRunsListed,
  refreshed,
} from './all-runs.js';
import {
  type Clock,
  formatDuration,
  formatRatio,
  formatWhen,
  ideasWord,
  resultWords,
} from './recent-words.js';
import {
  endReason,
  liveDetail,
  liveLook,
  liveStatus,
  type RowLook,
  type RunRowStatus,
} from './run-status.js';

/** The screen the main area shows. */
export type Screen =
  /**
   * New run; opened for a machine (Machines' "New run here"), it starts on that machine; opened
   * to run `again` a run, it starts from that run's place and options.
   */
  | {readonly kind: 'new-run'; readonly machine?: HostKey; readonly again?: RunRef}
  | {readonly kind: 'machines'}
  /**
   * Settings; opened for a machine's `section` (New run's link to set a machine up), it lands on
   * that machine and section, and leaving it returns to the screen it was opened from.
   */
  | {
      readonly kind: 'settings';
      readonly machine?: HostKey;
      readonly section?: 'clusters' | 'logins';
    }
  /** The Runs dashboard, the app's home. */
  | {readonly kind: 'runs'}
  | {readonly kind: 'usage'}
  | {readonly kind: 'run'; readonly run: RunRef};

/** Why a Recent row cannot say how its run came out, in words, and whether asking again may help. */
export interface RowProblem {
  readonly text: string;
  readonly retry: boolean;
}

export interface RunRow {
  readonly run: RunRef;
  /** Project and task: what a run is of. */
  readonly title: string;
  /**
   * What differs between runs of one title: for a Recent row, when it began and how it came out
   * (`07:32 · 3.83×`); for a live row, its machine and phase.
   */
  readonly detail: string;
  /** What else a Recent row did (`10 ideas · 52m`), after the detail; null when unknown. */
  readonly trail: string | null;
  /** All of it, for a tooltip: title, detail, trail, machine and run. */
  readonly tooltip: string;
  readonly status: RunRowStatus;
  /** The dot's tone and the row's status word. */
  readonly look: RowLook;
  /** Set when a Recent row's state cannot be read; the row says so and may offer a retry. */
  readonly problem: RowProblem | null;
  /** What the run is doing now, or why it ended, in one line; null when nothing says. */
  readonly now: string | null;
  /** When a live run's server started, in milliseconds; null for a run no server drives. */
  readonly startedAt: number | null;
  /** The run's digest as last read, with its chart data when the host offers it; null before one is. */
  readonly digest: ReadDigest | null;
}

/** What is known of one Recent run's digest: asked for, read, or not to be had. */
export type DigestSlot =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'read'; readonly entry: RunDigestEntry}
  /** The host's VibeSys does not offer digests. */
  | {readonly kind: 'unsupported'}
  /** The host did not answer for this run (it was unreachable, or the request failed). */
  | {readonly kind: 'failed'}
  /** The host answered and holds no record of this run: asking again cannot change that. */
  | {readonly kind: 'gone'};

/**
 * What is known of one live run's digest, which is read again while the run works: the latest
 * one read (none yet) and the request in flight for the next (none when idle).
 */
interface LiveDigest {
  readonly entry: Extract<RunDigestEntry, {kind: 'read'}> | null;
  readonly token: number | null;
}

/** What the sidebar's row menus ask of the app. */
export interface ShellActions {
  archive(run: RunRef): void;
  /**
   * Show `run` in a window of its own, where it is then live; this window leaves its Run screen
   * for New run when it showed that run.
   */
  openWindow(run: RunRef): void;
}

export interface SidebarState {
  /** Whether the overview (the first listings and Recent) has been heard. */
  readonly overviewHeard: boolean;
  /** The latest listing of each host, one per host. */
  readonly listings: readonly HostListing[];
  readonly recent: readonly RecentRun[];
  /** Saved runs the app could not read, so Recent leaves them out; empty when it read them all. */
  readonly skippedRecent: readonly SkippedRecent[];
  readonly archived: readonly RunRef[];
  /** What each machine lists of its projects' runs, for the All runs screen. */
  readonly allRuns: AllRunsState;
  /** Each shown Recent run's digest, by `digestKey`. */
  readonly digests: Readonly<Record<string, DigestSlot>>;
  /** Each live run's digest, by `digestKey`: its ideas and best so far, as the run page says them. */
  readonly live: Readonly<Record<string, LiveDigest>>;
  /** The last token a digest request used, so each is new. */
  readonly lastDigestToken: number;
  readonly selection: Screen;
  /** Screen to return to when Settings is opened, including its run or New run context. */
  readonly beforeSettings: Screen;
}

export type SidebarAction =
  | {
      readonly type: 'overview';
      readonly listings: readonly HostListing[];
      readonly recent: readonly RecentRun[];
      readonly skippedRecent: readonly SkippedRecent[];
    }
  | {readonly type: 'listing'; readonly listing: HostListing}
  | {readonly type: 'recent'; readonly recent: readonly RecentRun[]}
  | {readonly type: 'archived'; readonly archived: readonly RunRef[]}
  | {readonly type: 'select'; readonly screen: Screen}
  | {readonly type: 'back-from-settings'}
  /** What the host said of the digests asked under `token`; null when it could not be asked. */
  | {readonly type: 'digests-read'; readonly token: number; readonly reply: RunDigestsReply | null}
  /** The user asks again for a Recent run whose state could not be read. */
  | {readonly type: 'retry-digest'; readonly run: RunRef}
  /** The live runs' digests are due again: each live run's digest is read afresh. */
  | {readonly type: 'refresh-live-digests'}
  /** What a machine said of one project's runs; null when it could not say. */
  | {
      readonly type: 'project-runs-listed';
      readonly token: number;
      readonly host: HostKey;
      readonly project: string;
      readonly runs: readonly ListedRun[] | null;
    };

/** What the sidebar asks of the main process. */
export type SidebarRequest =
  /** Read what these runs of `project` on `host` say at a glance. */
  | {
      readonly kind: 'read-digests';
      readonly token: number;
      readonly host: HostKey;
      readonly project: string;
      readonly runIds: readonly string[];
    }
  | ListProjectRuns;

export type SidebarStep = Step<SidebarState, SidebarRequest>;

export const SIDEBAR_EMPTY: SidebarState = {
  overviewHeard: false,
  listings: [],
  recent: [],
  skippedRecent: [],
  archived: [],
  allRuns: ALL_RUNS_EMPTY,
  digests: {},
  live: {},
  lastDigestToken: 0,
  selection: {kind: 'runs'},
  beforeSettings: {kind: 'runs'},
};

function same(state: SidebarState): SidebarStep {
  return {state, requests: []};
}

/** The sidebar before anything is heard. */
export function initialSidebar(): SidebarStep {
  return same(SIDEBAR_EMPTY);
}

/** The selection, remembering the screen Settings was opened from. */
function selected(state: SidebarState, screen: Screen): SidebarStep {
  const beforeSettings =
    screen.kind === 'settings' && state.selection.kind !== 'settings'
      ? state.selection
      : state.beforeSettings;
  const entering = screen.kind === 'runs' && state.selection.kind !== 'runs';
  const next = {
    ...state,
    selection: screen,
    beforeSettings,
    ...(entering ? {allRuns: refreshed(state.allRuns)} : {}),
  };
  return same(next);
}

/** How many runs Recent shows before "Show all". */
export const RECENT_ROWS = 20;

function withListing(listings: readonly HostListing[], listing: HostListing): HostListing[] {
  return [...listings.filter(each => each.host !== listing.host), listing];
}

/** The key of a run's digest slot. */
export function digestKey(run: RunRef): string {
  return `${run.host} ${run.runId}`;
}

/** The next state for `action`, and the requests it makes. */
export function sidebarStep(state: SidebarState, action: SidebarAction): SidebarStep {
  const step = baseStep(state, action);
  const listed = askAllRuns(step.state);
  const asked = askDigests(listed.state);
  const live = askLiveDigests(asked.state, action.type === 'refresh-live-digests');
  return {
    state: live.state,
    requests: [...step.requests, ...listed.requests, ...asked.requests, ...live.requests],
  };
}

/** The Recent runs the sidebar lists: not archived, not live, newest first (all of them). */
function recentRuns(state: SidebarState): RecentRun[] {
  const live = liveRows(state);
  return state.recent
    .filter(recent => !state.archived.some(archived => sameRun(archived, recent)))
    .filter(recent => !live.some(row => sameRun(row.run, recent)))
    .sort((left, right) => right.attachedAt - left.attachedAt);
}

/** A run whose digest a row of the sidebar or the All runs screen shows. */
interface WantedDigest {
  readonly host: HostKey;
  readonly project: string;
  readonly runId: string;
}

/** The most runs the dashboard shows (the newest), so what it reads stays bounded. */
export const DASHBOARD_RUNS = 60;

/** The runs the dashboard shows: not archived, newest first, at most `DASHBOARD_RUNS`. */
export function dashboardRefs(state: SidebarState): ReturnType<typeof listedRunRefs> {
  return listedRunRefs(state.allRuns)
    .filter(
      ({host, run}) =>
        !state.archived.some(archived => sameRun(archived, {host, runId: run.runId})),
    )
    .sort(
      (left, right) =>
        right.run.createdAt - left.run.createdAt || (left.run.runId < right.run.runId ? 1 : -1),
    )
    .slice(0, DASHBOARD_RUNS);
}

/** The runs whose digest a screen shows now: the runs the user opened, and every run the dashboard shows. */
function shownRuns(state: SidebarState): WantedDigest[] {
  const recent = recentRuns(state).slice(0, RECENT_ROWS);
  const all = dashboardRefs(state);
  return [...recent, ...all.map(({host, project, run}) => ({host, project, runId: run.runId}))];
}

/** The most runs one digest request names (the helper's bound). */
const DIGESTS_PER_REQUEST = 100;

/**
 * Requests for `wanted` digests, in groups of at most `DIGESTS_PER_REQUEST` runs of one project on
 * one host, each with a token after `last`.
 */
function digestRequests(
  wanted: readonly WantedDigest[],
  last: number,
): {readonly requests: Extract<SidebarRequest, {kind: 'read-digests'}>[]; readonly token: number} {
  const groups = new Map<string, WantedDigest[]>();
  for (const run of wanted) {
    const key = JSON.stringify([run.host, run.project]);
    groups.set(key, [...(groups.get(key) ?? []), run]);
  }
  const requests: Extract<SidebarRequest, {kind: 'read-digests'}>[] = [];
  let token = last;
  for (const [first, ...rest] of [...groups.values()].map(group => group)) {
    if (first === undefined) continue;
    const group = [first, ...rest];
    for (let at = 0; at < group.length; at += DIGESTS_PER_REQUEST) {
      token += 1;
      const runIds = group.slice(at, at + DIGESTS_PER_REQUEST).map(run => run.runId);
      requests.push({
        kind: 'read-digests',
        token,
        host: first.host,
        project: first.project,
        runIds,
      });
    }
  }
  return {requests, token};
}

/**
 * Digests for the rows that show and have none, and a run that is live again forgets its digest,
 * so it is read afresh when it ends. A live run is never asked for here: its row reads its digest
 * through `askLiveDigests`, and asking for it here would be forgotten again by this very step.
 */
function askDigests(state: SidebarState): SidebarStep {
  const live = liveRows(state);
  const digests: Record<string, DigestSlot> = {};
  for (const [key, slot] of Object.entries(state.digests)) {
    if (!live.some(row => digestKey(row.run) === key)) digests[key] = slot;
  }
  const wanted = shownRuns(state).filter(run => {
    const key = digestKey({host: run.host, runId: run.runId});
    return digests[key] === undefined && !live.some(row => digestKey(row.run) === key);
  });
  if (wanted.length === 0) {
    const unchanged = Object.keys(digests).length === Object.keys(state.digests).length;
    return same(unchanged ? state : {...state, digests});
  }
  const {requests, token} = digestRequests(wanted, state.lastDigestToken);
  for (const request of requests) {
    for (const runId of request.runIds) {
      digests[digestKey({host: request.host, runId})] = {kind: 'loading', token: request.token};
    }
  }
  return {state: {...state, digests, lastDigestToken: token}, requests};
}

/**
 * The digest of every live run: asked when the run first shows live and again on each
 * `refresh-live-digests`, one request at a time per run; the one before it stays on the row until
 * the next answers, and a run that is no longer live is forgotten.
 */
function askLiveDigests(state: SidebarState, refresh: boolean): SidebarStep {
  const wanted = state.listings.flatMap(listing =>
    listing.kind !== 'listed'
      ? []
      : [...newestByRun(listing.live).values()].map(server => ({
          host: listing.host,
          project: server.projectRoot,
          runId: server.runId,
        })),
  );
  const live: Record<string, LiveDigest> = {};
  const due: WantedDigest[] = [];
  for (const run of wanted) {
    const key = digestKey({host: run.host, runId: run.runId});
    const known = state.live[key];
    if (known === undefined || (refresh && known.token === null)) due.push(run);
    live[key] = known ?? {entry: null, token: null};
  }
  const forgotten = Object.keys(state.live).length !== Object.keys(live).length;
  if (due.length === 0) return same(forgotten ? {...state, live} : state);
  const {requests, token} = digestRequests(due, state.lastDigestToken);
  for (const request of requests) {
    for (const runId of request.runIds) {
      const key = digestKey({host: request.host, runId});
      live[key] = {entry: live[key]?.entry ?? null, token: request.token};
    }
  }
  return {state: {...state, live, lastDigestToken: token}, requests};
}

/** The project listings the dashboard still needs: every known project's runs, on every screen. */
function askAllRuns(state: SidebarState): SidebarStep {
  const asked = askProjectRuns(state.allRuns, state.listings, state.recent);
  return asked.requests.length === 0
    ? same(state)
    : {state: {...state, allRuns: asked.all}, requests: asked.requests};
}

/** What the host said of the digests asked under `token`, in each slot that awaited it. */
function digestsRead(
  state: SidebarState,
  action: Extract<SidebarAction, {type: 'digests-read'}>,
): SidebarStep {
  return same({
    ...state,
    digests: recentDigestsRead(state.digests, action),
    live: liveDigestsRead(state.live, action),
  });
}

function recentDigestsRead(
  slots: SidebarState['digests'],
  action: Extract<SidebarAction, {type: 'digests-read'}>,
): Record<string, DigestSlot> {
  const digests: Record<string, DigestSlot> = {};
  for (const [key, slot] of Object.entries(slots)) {
    if (slot.kind !== 'loading' || slot.token !== action.token) {
      digests[key] = slot;
      continue;
    }
    const runId = key.slice(key.indexOf(' ') + 1);
    const {reply} = action;
    const entry = reply?.kind === 'digests' ? reply.runs.get(runId) : undefined;
    const absent: DigestSlot = reply?.kind === 'digests' ? {kind: 'gone'} : {kind: 'failed'};
    digests[key] =
      reply?.kind === 'unsupported'
        ? {kind: 'unsupported'}
        : entry === undefined
          ? absent
          : {kind: 'read', entry};
  }
  return digests;
}

/** A live run's answer replaces its figures when it read; any other answer leaves the last ones. */
function liveDigestsRead(
  slots: SidebarState['live'],
  action: Extract<SidebarAction, {type: 'digests-read'}>,
): Record<string, LiveDigest> {
  const live: Record<string, LiveDigest> = {};
  for (const [key, known] of Object.entries(slots)) {
    if (known.token !== action.token) {
      live[key] = known;
      continue;
    }
    const entry =
      action.reply?.kind === 'digests'
        ? action.reply.runs.get(key.slice(key.indexOf(' ') + 1))
        : undefined;
    live[key] = {entry: entry?.kind === 'read' ? entry : known.entry, token: null};
  }
  return live;
}

/** A run asked for again: its slot is forgotten, so the next step reads it. */
function retryDigest(state: SidebarState, run: RunRef): SidebarStep {
  const forgotten = digestKey(run);
  const digests = Object.fromEntries(
    Object.entries(state.digests).filter(([key]) => key !== forgotten),
  );
  return same({...state, digests});
}

function baseStep(state: SidebarState, action: SidebarAction): SidebarStep {
  switch (action.type) {
    case 'digests-read':
      return digestsRead(state, action);
    case 'retry-digest':
      return retryDigest(state, action.run);
    case 'refresh-live-digests':
      return same(state);
    case 'project-runs-listed':
      return same({...state, allRuns: projectRunsListed(state.allRuns, action)});
    case 'overview':
      return same({
        ...state,
        overviewHeard: true,
        listings: action.listings.reduce<HostListing[]>(withListing, []),
        recent: action.recent,
        skippedRecent: action.skippedRecent,
      });
    case 'listing':
      return same({...state, listings: withListing(state.listings, action.listing)});
    // A pushed list is of a file the store has just written, which it writes only when all is readable.
    case 'recent':
      return same({...state, recent: action.recent, skippedRecent: []});
    case 'archived':
      return same({...state, archived: action.archived});
    case 'select':
      return selected(state, action.screen);
    case 'back-from-settings':
      return selected(state, state.beforeSettings);
  }
}

function projectName(path: string): string {
  return path.split('/').filter(Boolean).at(-1) ?? path;
}

function label(host: HostKey): string {
  return hostLabel(hostFromKey(host));
}

/** The newest record of each run in `runs`, by run id. */
function newestByRun(runs: readonly HostRun[]): Map<string, HostRun> {
  const newest = new Map<string, HostRun>();
  for (const run of runs) {
    const known = newest.get(run.runId);
    if (known === undefined || known.startedAt < run.startedAt) newest.set(run.runId, run);
  }
  return newest;
}

function titleOf(
  state: SidebarState,
  run: RunRef,
  project: string,
  listedTask: string | null = null,
): string {
  const task = state.recent.find(recent => sameRun(recent, run))?.task ?? listedTask;
  const name = projectName(project);
  return task === null ? name : `${name} · ${task}`;
}

/** The runs a live server drives now, on every listed host. */
export function liveRows(state: SidebarState): RunRow[] {
  return state.listings.flatMap(listing => {
    if (listing.kind !== 'listed') return [];
    return [...newestByRun(listing.live).values()].map(server => {
      const run: RunRef = {host: listing.host, runId: server.runId};
      const title = titleOf(state, run, server.projectRoot);
      const detail = `${label(listing.host)} · ${liveDetail(server)}`;
      const trail = liveTrail(state.live[digestKey(run)]?.entry ?? null);
      return {
        run,
        title,
        detail,
        trail,
        tooltip: [title, trail === null ? detail : `${detail} · ${trail}`, `run ${run.runId}`].join(
          '\n',
        ),
        status: liveStatus(server),
        look: liveLook(server),
        problem: null,
        now: liveDetail(server),
        startedAt: server.startedAt * 1000,
        digest: state.live[digestKey(run)]?.entry ?? null,
      };
    });
  });
}

/**
 * What a live run has done so far, as its page says it: its ideas, the best result so far (the
 * adopted one once the run is adopting), and how long it has gone on; null before a digest is read.
 */
function liveTrail(read: ReadDigest | null): string | null {
  if (read === null) return null;
  const best = read.adoptedRatio ?? read.bestSoFarRatio;
  const parts = [
    ideasTrail(read.ideas),
    ...(best === null
      ? []
      : [`${formatRatio(best)} ${read.adoptedRatio === null ? 'best so far' : 'adopted'}`]),
    ...(read.durationSeconds === null ? [] : [formatDuration(read.durationSeconds)]),
  ];
  return parts.join(' · ');
}

interface Reading {
  readonly look: RowLook;
  readonly status: RunRowStatus;
  readonly result: string;
  readonly problem: RowProblem | null;
  /** How it ended, when something says. */
  readonly kind: EndKind | undefined;
}

/** How a Recent run came out: what the registry or its digest says, or why nothing does. */
function reading(
  found: EndLook | null,
  read: ReadDigest | undefined,
  slot: DigestSlot | undefined,
  listing: HostListing | undefined,
): Reading {
  if (found !== null) {
    const look: RowLook = {tone: found.tone, word: found.label.toLowerCase()};
    const result = resultWords({
      kind: found.kind,
      word: look.word,
      ok: found.tone === 'ok',
      ratio: read?.adoptedRatio ?? null,
      ideas: read?.ideas ?? null,
    });
    return {look, status: found.kind, result, problem: null, kind: found.kind};
  }
  if (read !== undefined) {
    // The journal never says the run ended and no server drives it: its server died unannounced.
    const look: RowLook = {tone: 'warn', word: 'stopped unexpectedly'};
    return {look, status: 'stopped', result: look.word, problem: null, kind: undefined};
  }
  const problem = recentProblem(slot, listing);
  const word = problem === null ? 'reading…' : problem.text;
  return {
    look: {tone: 'neutral', word},
    status: 'unknown',
    result: word,
    problem,
    kind: undefined,
  };
}

/**
 * When a Recent run began, in words: its digest's creation time, else its server's start; null when
 * neither is a moment a run could have begun, so an unknown time is never worded as a date.
 */
function beganAt(
  entry: RunDigestEntry | undefined,
  ended: HostRun | undefined,
  clock: Clock,
): string | null {
  const created = entry === undefined ? null : formatWhen(entry.createdAt, clock);
  return created ?? (ended === undefined ? null : formatWhen(ended.startedAt * 1000, clock));
}

/** What the Recent row of `recent` says, from the registry's record of its end and its digest. */
export function recentRow(state: SidebarState, recent: RecentRun, clock: Clock): RunRow {
  const listing = state.listings.find(each => each.host === recent.host);
  const ended =
    listing?.kind === 'listed' ? newestByRun(listing.ended).get(recent.runId) : undefined;
  const run: RunRef = {host: recent.host, runId: recent.runId};
  const title = titleOf(state, run, recent.project, recent.task);
  const slot = state.digests[digestKey(run)];
  const entry = slot?.kind === 'read' ? slot.entry : undefined;
  const read = entry?.kind === 'read' ? entry : undefined;
  const registered = ended === undefined ? null : endedLook(ended);
  const found = registered ?? (read === undefined || !read.over ? null : digestLook(read));
  const {look, status, result, problem, kind} = reading(found, read, slot, listing);
  const began = beganAt(entry, ended, clock);
  const detail = began === null ? result : `${began} · ${result}`;
  const trail = read === undefined ? null : trailWords(read, kind);
  const reason = ended === undefined ? null : endReason(ended);
  const tooltip = [
    title,
    trail === null ? detail : `${detail} · ${trail}`,
    ...(reason === null ? [] : [reason]),
    `${label(recent.host)} · run ${recent.runId}`,
  ].join('\n');
  return {
    run,
    title,
    detail,
    trail,
    tooltip,
    status,
    look,
    problem,
    now: reason,
    startedAt: null,
    digest: read ?? null,
  };
}

/** A digest that was read. */
export type ReadDigest = Extract<RunDigestEntry, {kind: 'read'}>;

/** A read digest's ideas as the trail says them; null: the journal predates the run trace. */
function ideasTrail(ideas: number | null): string {
  return ideas === null ? 'ideas not recorded' : ideasWord(ideas);
}

/** What more a read digest says: its ideas (unless the result already does) and how long it ran. */
function trailWords(read: ReadDigest, kind: EndKind | undefined): string | null {
  const parts = [
    ...(kind === 'failed' || kind === 'stopped' ? [] : [ideasTrail(read.ideas)]),
    ...(read.durationSeconds === null ? [] : [formatDuration(read.durationSeconds)]),
  ];
  return parts.length === 0 ? null : parts.join(' · ');
}

/** A row's problem when its machine has one: that status's word, lower case (`problemWord`). */
function machineProblemRow(problem: MachineProblem): RowProblem {
  return {text: problemWord(problem).toLowerCase(), retry: problem !== 'needs-update'};
}

/** Why a Recent run's state is not known: nothing is read yet (null), or it cannot be. */
function recentProblem(
  slot: DigestSlot | undefined,
  listing: HostListing | undefined,
): RowProblem | null {
  if (slot === undefined || slot.kind === 'loading') return null;
  if (slot.kind === 'unsupported') return machineProblemRow('needs-update');
  if (slot.kind === 'gone') return {text: RUN_GONE_WORDS, retry: false};
  if (listing?.kind === 'unavailable') {
    return machineProblemRow(connectionProblem(listing.connection) ?? 'unavailable');
  }
  return {text: "can't read run state", retry: true};
}

/**
 * Recent: the runs this user opened that no server drives now and that are not archived, newest
 * first, at most `RECENT_ROWS`; `more` counts the rest. `clock` words the times.
 */
export function recentRows(
  state: SidebarState,
  clock: Clock = {now: Date.UTC(2026, 0, 1), timeZone: 'UTC'},
): {readonly rows: RunRow[]; readonly more: number} {
  const all = recentRuns(state);
  return {
    rows: all.slice(0, RECENT_ROWS).map(recent => recentRow(state, recent, clock)),
    more: Math.max(0, all.length - RECENT_ROWS),
  };
}

function recentProblems(state: SidebarState, live: readonly RunRow[]): string[] {
  const problems: string[] = [];
  const {rows} = recentRows(state);
  if (rows.length > RECENT_ROWS) problems.push('Recent shows more than its limit');
  for (const row of rows) {
    if (state.archived.some(archived => sameRun(archived, row.run))) {
      problems.push(`archived ${row.run.runId} is in Recent`);
    }
    if (live.some(each => sameRun(each.run, row.run))) {
      problems.push(`${row.run.runId} is both live and recent`);
    }
    if (row.status === 'live' || row.status === 'paused') {
      problems.push(`recent ${row.run.runId} reads as live`);
    }
    if (state.digests[digestKey(row.run)] === undefined) {
      problems.push(`recent ${row.run.runId} has no digest asked for`);
    }
  }
  return problems;
}

function dashboardProblems(state: SidebarState, live: readonly RunRow[]): string[] {
  const problems: string[] = [];
  const refs = dashboardRefs(state);
  if (refs.length > DASHBOARD_RUNS) problems.push('the dashboard shows more than its limit');
  for (const {host, run} of refs) {
    const ref: RunRef = {host, runId: run.runId};
    if (state.archived.some(archived => sameRun(archived, ref))) {
      problems.push(`archived ${run.runId} is on the dashboard`);
    }
    const isLive = live.some(each => sameRun(each.run, ref));
    if (!isLive && state.digests[digestKey(ref)] === undefined) {
      problems.push(`${run.runId} has no digest asked for`);
    }
  }
  return problems;
}

function liveProblems(live: readonly RunRow[]): string[] {
  const problems: string[] = [];
  for (const row of live) {
    if (row.status !== 'live' && row.status !== 'paused') {
      problems.push(`live ${row.run.runId} reads as ended`);
    }
    if (live.filter(each => sameRun(each.run, row.run)).length > 1) {
      problems.push(`${row.run.runId} is live twice`);
    }
  }
  return problems;
}

/** What must hold in every state; empty when all do. */
export function sidebarInvariants(state: SidebarState): string[] {
  const hosts = state.listings.map(listing => listing.host);
  const live = liveRows(state);
  return [
    ...(new Set(hosts).size === hosts.length ? [] : ['a host is listed twice']),
    ...recentProblems(state, live),
    ...dashboardProblems(state, live),
    ...liveProblems(live),
  ];
}
