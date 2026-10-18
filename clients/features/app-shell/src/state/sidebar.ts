/**
 * The sidebar's store: a pure reducer over what the hosts' watches push (their listings), the runs
 * this user opened (Recent), the runs archived, the saved machines and whether each is ready, and
 * which screen is shown. The rows the sidebar draws are selectors over it, so a push is folded once
 * and every row follows. The machines are the saved ones (`hosts.json`, the same list the Machines
 * screen reads), listed at start and again when the user leaves the Machines screen, where they
 * may have changed; each listing and check carries a token, so a late reply is dropped.
 */
import {
  digestLook,
  type EndKind,
  type EndLook,
  endedLook,
  type HostKey,
  type HostListing,
  type HostRun,
  hostFromKey,
  hostLabel,
  type RunDigestEntry,
  type RunDigestsReply,
} from '@vibesys/hosts';
import {type RecentRun, type RunRef, sameRun} from '@vibesys/runs';
import type {Step} from '@vibesys/store';
import {type Clock, formatDuration, formatWhen, ideasWord, resultWords} from './recent-words.js';
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
  | {readonly kind: 'settings'}
  | {readonly kind: 'all-runs'}
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
}

/** Runs from one machine, in recency order. */
export interface MachineRunGroup {
  readonly host: HostKey;
  readonly title: string;
  readonly rows: readonly RunRow[];
}

/** What is known of one Recent run's digest: asked for, read, or not to be had. */
export type DigestSlot =
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'read'; readonly entry: RunDigestEntry}
  /** The host's VibeSys does not offer digests. */
  | {readonly kind: 'unsupported'}
  /** The host did not answer for this run (unreachable, or it has no record of it). */
  | {readonly kind: 'failed'};

/** What the sidebar's row menus ask of the app. */
export interface ShellActions {
  archive(run: RunRef): void;
  /**
   * Show `run` in a window of its own, where it is then live; this window leaves its Run screen
   * for New run when it showed that run.
   */
  openWindow(run: RunRef): void;
}

/** One saved machine: whether its last check found it ready, or the check awaited. */
export interface SidebarMachine {
  readonly host: HostKey;
  /** Null until a check answers. */
  readonly ready: boolean | null;
  readonly checking: number | null;
}

export interface SidebarMachines {
  /** The listing awaited, or null. */
  readonly listing: number | null;
  readonly saved: readonly SidebarMachine[];
  /** The last token issued, so each is new. */
  readonly lastToken: number;
}

export interface SidebarState {
  /** The latest listing of each host, one per host. */
  readonly listings: readonly HostListing[];
  readonly recent: readonly RecentRun[];
  readonly archived: readonly RunRef[];
  readonly machines: SidebarMachines;
  /** Each shown Recent run's digest, by `digestKey`. */
  readonly digests: Readonly<Record<string, DigestSlot>>;
  /** The last token a digest request used, so each is new. */
  readonly lastDigestToken: number;
  readonly selection: Screen;
  /** Screen to return to when Settings is opened, including its run or New run context. */
  readonly beforeSettings: Screen;
  /** Whether the sidebar is shown; the viewer may hide it completely. */
  readonly shown: boolean;
}

export type SidebarAction =
  | {
      readonly type: 'overview';
      readonly listings: readonly HostListing[];
      readonly recent: readonly RecentRun[];
    }
  | {readonly type: 'listing'; readonly listing: HostListing}
  | {readonly type: 'recent'; readonly recent: readonly RecentRun[]}
  | {readonly type: 'archived'; readonly archived: readonly RunRef[]}
  | {readonly type: 'select'; readonly screen: Screen}
  | {readonly type: 'back-from-settings'}
  | {readonly type: 'toggle-sidebar'}
  /** What the host said of the digests asked under `token`; null when it could not be asked. */
  | {readonly type: 'digests-read'; readonly token: number; readonly reply: RunDigestsReply | null}
  /** The user asks again for a Recent run whose state could not be read. */
  | {readonly type: 'retry-digest'; readonly run: RunRef}
  /** The saved machines, or why they could not be read. */
  | {
      readonly type: 'machines-listed';
      readonly token: number;
      readonly hosts: readonly HostKey[] | null;
    }
  /** Whether one machine's check found it ready (`machine` answered `ready`). */
  | {
      readonly type: 'machine-checked';
      readonly token: number;
      readonly host: HostKey;
      readonly ready: boolean;
    };

/** What the sidebar asks of the main process: the saved machines, and one machine's check. */
export type SidebarRequest =
  | {readonly kind: 'list-machines'; readonly token: number}
  /** Read what these runs of `project` on `host` say at a glance. */
  | {
      readonly kind: 'read-digests';
      readonly token: number;
      readonly host: HostKey;
      readonly project: string;
      readonly runIds: readonly string[];
    }
  | {readonly kind: 'check-machine'; readonly token: number; readonly host: HostKey};

export type SidebarStep = Step<SidebarState, SidebarRequest>;

export const SIDEBAR_EMPTY: SidebarState = {
  listings: [],
  recent: [],
  archived: [],
  machines: {listing: null, saved: [], lastToken: 0},
  digests: {},
  lastDigestToken: 0,
  selection: {kind: 'new-run'},
  beforeSettings: {kind: 'new-run'},
  shown: true,
};

function listMachines(state: SidebarState): SidebarStep {
  const token = state.machines.lastToken + 1;
  return {
    state: {...state, machines: {...state.machines, listing: token, lastToken: token}},
    requests: [{kind: 'list-machines', token}],
  };
}

/** The sidebar before anything is heard, shown or hidden as the viewer left it: it asks for the saved machines. */
export function initialSidebar(shown = true): SidebarStep {
  return listMachines({...SIDEBAR_EMPTY, shown});
}

function same(state: SidebarState): SidebarStep {
  return {state, requests: []};
}

/** The listed machines, each checked again; what was known of one that stays is kept meanwhile. */
function machinesListed(
  state: SidebarState,
  action: Extract<SidebarAction, {type: 'machines-listed'}>,
): SidebarStep {
  const {machines} = state;
  if (machines.listing !== action.token) return same(state);
  if (action.hosts === null) return same({...state, machines: {...machines, listing: null}});
  let last = machines.lastToken;
  const requests: SidebarRequest[] = [];
  const saved = [...new Set(action.hosts)].map(host => {
    last += 1;
    requests.push({kind: 'check-machine', token: last, host});
    const known = machines.saved.find(machine => machine.host === host);
    return {host, ready: known?.ready ?? null, checking: last};
  });
  return {state: {...state, machines: {listing: null, saved, lastToken: last}}, requests};
}

function machineChecked(
  state: SidebarState,
  action: Extract<SidebarAction, {type: 'machine-checked'}>,
): SidebarStep {
  const machine = state.machines.saved.find(each => each.host === action.host);
  if (machine === undefined || machine.checking !== action.token) return same(state);
  const saved = state.machines.saved.map(each =>
    each === machine ? {...each, ready: action.ready, checking: null} : each,
  );
  return same({...state, machines: {...state.machines, saved}});
}

/** The selection, and a fresh listing when the user leaves the Machines screen. */
function selected(state: SidebarState, screen: Screen): SidebarStep {
  const beforeSettings =
    (screen.kind === 'settings' || screen.kind === 'machines') &&
    state.selection.kind !== 'settings' &&
    state.selection.kind !== 'machines'
      ? state.selection
      : state.beforeSettings;
  const next = {...state, selection: screen, beforeSettings};
  const leaving = state.selection.kind === 'machines' && screen.kind !== 'machines';
  return leaving ? listMachines(next) : same(next);
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
  const asked = askDigests(step.state);
  return {state: asked.state, requests: [...step.requests, ...asked.requests]};
}

/** The Recent runs the sidebar lists: not archived, not live, newest first (all of them). */
function recentRuns(state: SidebarState): RecentRun[] {
  const live = liveRows(state);
  return state.recent
    .filter(recent => !state.archived.some(archived => sameRun(archived, recent)))
    .filter(recent => !live.some(row => sameRun(row.run, recent)))
    .sort((left, right) => right.attachedAt - left.attachedAt);
}

/**
 * Digests for the Recent rows that show and have none: one request per project on a host, and a
 * run that is live again forgets its digest, so it is read afresh when it ends.
 */
function askDigests(state: SidebarState): SidebarStep {
  const live = liveRows(state);
  const digests: Record<string, DigestSlot> = {};
  for (const [key, slot] of Object.entries(state.digests)) {
    if (!live.some(row => digestKey(row.run) === key)) digests[key] = slot;
  }
  const wanted = recentRuns(state)
    .slice(0, RECENT_ROWS)
    .filter(recent => digests[digestKey(recent)] === undefined);
  if (wanted.length === 0) {
    const unchanged = Object.keys(digests).length === Object.keys(state.digests).length;
    return same(unchanged ? state : {...state, digests});
  }
  let token = state.lastDigestToken;
  const requests: SidebarRequest[] = [];
  const groups = new Map<string, RecentRun[]>();
  for (const recent of wanted) {
    const key = JSON.stringify([recent.host, recent.project]);
    groups.set(key, [...(groups.get(key) ?? []), recent]);
  }
  for (const runs of groups.values()) {
    token += 1;
    const [first] = runs;
    if (first === undefined) continue;
    requests.push({
      kind: 'read-digests',
      token,
      host: first.host,
      project: first.project,
      runIds: runs.map(run => run.runId),
    });
    for (const run of runs) digests[digestKey(run)] = {kind: 'loading', token};
  }
  return {state: {...state, digests, lastDigestToken: token}, requests};
}

/** What the host said of the digests asked under `token`, in each slot that awaited it. */
function digestsRead(
  state: SidebarState,
  action: Extract<SidebarAction, {type: 'digests-read'}>,
): SidebarStep {
  const digests: Record<string, DigestSlot> = {};
  for (const [key, slot] of Object.entries(state.digests)) {
    if (slot.kind !== 'loading' || slot.token !== action.token) {
      digests[key] = slot;
      continue;
    }
    const runId = key.slice(key.indexOf(' ') + 1);
    const {reply} = action;
    const entry = reply?.kind === 'digests' ? reply.runs.get(runId) : undefined;
    digests[key] =
      reply?.kind === 'unsupported'
        ? {kind: 'unsupported'}
        : entry === undefined
          ? {kind: 'failed'}
          : {kind: 'read', entry};
  }
  return same({...state, digests});
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
    case 'overview':
      return same({
        ...state,
        listings: action.listings.reduce<HostListing[]>(withListing, []),
        recent: action.recent,
      });
    case 'listing':
      return same({...state, listings: withListing(state.listings, action.listing)});
    case 'recent':
      return same({...state, recent: action.recent});
    case 'archived':
      return same({...state, archived: action.archived});
    case 'select':
      return selected(state, action.screen);
    case 'back-from-settings':
      return selected(state, state.beforeSettings);
    case 'toggle-sidebar':
      return same({...state, shown: !state.shown});
    case 'machines-listed':
      return machinesListed(state, action);
    case 'machine-checked':
      return machineChecked(state, action);
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

function titleOf(state: SidebarState, run: RunRef, project: string): string {
  const task = state.recent.find(recent => sameRun(recent, run))?.task ?? null;
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
      return {
        run,
        title,
        detail,
        trail: null,
        tooltip: `${title}\n${detail}\nrun ${run.runId}`,
        status: liveStatus(server),
        look: liveLook(server),
        problem: null,
      };
    });
  });
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
      ratio: read?.bestRatio ?? null,
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

/** When a Recent run began: its digest's creation time, else its server's start. */
function beganAt(entry: RunDigestEntry | undefined, ended: HostRun | undefined): number | null {
  if (entry !== undefined && Number.isFinite(entry.createdAt)) return entry.createdAt;
  return ended === undefined ? null : ended.startedAt * 1000;
}

/** What the Recent row of `recent` says, from the registry's record of its end and its digest. */
function recentRow(state: SidebarState, recent: RecentRun, clock: Clock): RunRow {
  const listing = state.listings.find(each => each.host === recent.host);
  const ended =
    listing?.kind === 'listed' ? newestByRun(listing.ended).get(recent.runId) : undefined;
  const run: RunRef = {host: recent.host, runId: recent.runId};
  const title = titleOf(state, run, recent.project);
  const slot = state.digests[digestKey(run)];
  const entry = slot?.kind === 'read' ? slot.entry : undefined;
  const read = entry?.kind === 'read' ? entry : undefined;
  const registered = ended === undefined ? null : endedLook(ended);
  const found = registered ?? (read === undefined || !read.over ? null : digestLook(read));
  const {look, status, result, problem, kind} = reading(found, read, slot, listing);
  const began = beganAt(entry, ended);
  const detail = began === null ? result : `${formatWhen(began, clock)} · ${result}`;
  const trail = read === undefined ? null : trailWords(read, kind);
  const reason = ended === undefined ? null : endReason(ended);
  const tooltip = [
    title,
    trail === null ? detail : `${detail} · ${trail}`,
    ...(reason === null ? [] : [reason]),
    `${label(recent.host)} · run ${recent.runId}`,
  ].join('\n');
  return {run, title, detail, trail, tooltip, status, look, problem};
}

/** A digest that was read. */
type ReadDigest = Extract<RunDigestEntry, {kind: 'read'}>;

/** What more a read digest says: its ideas (unless the result already does) and how long it ran. */
function trailWords(read: ReadDigest, kind: EndKind | undefined): string | null {
  const parts = [
    ...(kind === 'failed' || kind === 'stopped' ? [] : [ideasWord(read.ideas)]),
    ...(read.durationSeconds === null ? [] : [formatDuration(read.durationSeconds)]),
  ];
  return parts.length === 0 ? null : parts.join(' · ');
}

/** Why a Recent run's state is not known: nothing is read yet (null), or it cannot be. */
function recentProblem(
  slot: DigestSlot | undefined,
  listing: HostListing | undefined,
): RowProblem | null {
  if (slot === undefined || slot.kind === 'loading') return null;
  if (slot.kind === 'unsupported') return {text: 'machine needs a newer VibeSys', retry: false};
  if (listing?.kind === 'unavailable') return {text: "can't reach the machine", retry: true};
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

/** Recent's rows grouped by machine; recency order holds within each machine. */
export function recentGroups(
  state: SidebarState,
  clock: Clock,
): {readonly groups: readonly MachineRunGroup[]; readonly more: number} {
  const {rows, more} = recentRows(state, clock);
  const groups: {host: HostKey; title: string; rows: RunRow[]}[] = [];
  for (const row of rows) {
    let machine = groups.find(group => group.host === row.run.host);
    if (machine === undefined) {
      machine = {host: row.run.host, title: label(row.run.host), rows: []};
      groups.push(machine);
    }
    machine.rows.push(row);
  }
  return {groups, more};
}

/** Live rows grouped by their machine, preserving listing and run order. */
export function liveMachineGroups(
  state: SidebarState,
): readonly {title: string; rows: readonly RunRow[]}[] {
  const groups: {host: HostKey; title: string; rows: RunRow[]}[] = [];
  for (const row of liveRows(state)) {
    let group = groups.find(each => each.host === row.run.host);
    if (group === undefined) {
      group = {host: row.run.host, title: label(row.run.host), rows: []};
      groups.push(group);
    }
    group.rows.push(row);
  }
  return groups;
}

/**
 * The machines the sidebar counts: every saved machine, and those whose last check found them not
 * ready (checkout not found, sign-in, or unreachable).
 */
export function machineCounts(state: SidebarState): {
  readonly count: number;
  readonly attention: number;
} {
  const {saved} = state.machines;
  return {count: saved.length, attention: saved.filter(machine => machine.ready === false).length};
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
  const machines = state.machines.saved.map(machine => machine.host);
  const {attention, count} = machineCounts(state);
  return [
    ...(new Set(hosts).size === hosts.length ? [] : ['a host is listed twice']),
    ...(new Set(machines).size === machines.length ? [] : ['a machine is counted twice']),
    ...(attention <= count ? [] : ['more machines need attention than are saved']),
    ...recentProblems(state, live),
    ...liveProblems(live),
  ];
}
