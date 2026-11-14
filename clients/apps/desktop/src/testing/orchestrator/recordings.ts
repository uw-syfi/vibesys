/**
 * A `Recording` of the orchestrator service played by a `FakeDaemon`: the same answers the daemon
 * would give, as encoded frames, so everything above the transport runs for real. Faults of the
 * link are `withFaults` of `testing/transport`; faults only the daemon can cause (a command it
 * applied but whose reply was lost) are `DaemonOptions`.
 */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {timestampMs} from '@bufbuild/protobuf/wkt';
import {
  BuildVersionsSchema,
  CheckRepositoryRequestSchema,
  CheckRepositoryResponseSchema,
  Compatibility,
  DispatchRequestSchema,
  DispatchResponseSchema,
  GetItemRequestSchema,
  GetItemResponseSchema,
  GetRunHistoryRequestSchema,
  GetRunHistoryResponseSchema,
  HelloResponseSchema,
  HostControlService,
  type IdentifyResponse,
  IdentifyResponseSchema,
  ItemsPageSchema,
  type ListCatalogueResponse,
  ListCatalogueResponseSchema,
  ListDirectoryRequestSchema,
  ListDirectoryResponseSchema,
  ListItemsRequestSchema,
  ListItemsResponseSchema,
  OrchestratorService,
  oneofIs,
  type RunChange,
  type RunSummary,
  RunSummarySchema,
  SnapshotDetail,
  WatchAgentRequestSchema,
  WatchAgentResponseSchema,
  WatchHostCostRequestSchema,
  WatchHostCostResponseSchema,
  WatchRunCostRequestSchema,
  WatchRunCostResponseSchema,
  WatchRunsRequestSchema,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import {RpcStatus} from '../../core/errors.js';
import type {CallTarget} from '../../core/transport/gateway-transport.js';
import {invokeCallback} from '../callback-errors.js';
import type {Recording, Step} from '../transport/index.js';
import type {FakeDaemon} from './fake-daemon.js';
import type {FakeFiles} from './fake-files.js';
import {newRunSteps} from './new-run-steps.js';

/** What happens to the `nth` Dispatch call. */
export type DispatchFate =
  /** The daemon carries it out and replies. */
  | 'ok'
  /** The request never reaches the daemon: the link fails first. */
  | 'lose-request'
  /** The daemon carries it out, and the reply is lost on the way back. */
  | 'lose-reply';

export interface DaemonOptions {
  /** The answer to Hello; COMPATIBLE by default. */
  readonly compatibility?: Compatibility;
  /** Per Hello call (0-based): fail it with this status instead of answering. */
  readonly helloFails?: (nth: number) => number | undefined;
  /** What happens to each Dispatch call; every call succeeds by default. */
  readonly dispatch?: (nth: number) => DispatchFate;
  /** A stream ends cleanly after its frames instead of staying open. */
  readonly closeStreams?: boolean;
  /** How many of a run's messages one page of its history holds; the host's own bound by default. */
  readonly historyPageMessages?: number;
  /**
   * The run stream stays open and carries each change the daemon commits after it opened, as the
   * real host's does, instead of holding its first frames for ever.
   */
  readonly live?: boolean;
  /** What the host's control service answers to Identify; with none it is not served (UNIMPLEMENTED). */
  readonly identify?: () => IdentifyResponse;
  /**
   * The host's refusal of a repository path for a start (its reason, and whether it is the host's
   * fault rather than the path's), or undefined to accept it; every path is accepted by default.
   * `unserved` makes the method unknown, as an older host's is.
   */
  readonly checkRepository?:
    | ((repository: string) => {readonly reason: string; readonly notReady?: boolean} | undefined)
    | 'unserved';
  /**
   * The host's file system. With it the host serves `ListDirectory` and answers `CheckRepository`
   * from it (its refusals, and the tasks of a repository that passes), unless `checkRepository` says
   * otherwise; without it `ListDirectory` is unknown, as an older host's is, and no path has tasks.
   */
  readonly files?: FakeFiles;
  /** What `ListCatalogue` answers; the fixture's rich catalogue (see `new-run-fixtures`) by default. */
  readonly catalogue?: ListCatalogueResponse;
}

const PREFIX = `/${OrchestratorService.typeName}/`;

/** The fixed protocol revision the fake daemon speaks. */
const FAKE_DAEMON = {binaryVersion: 'fake', protocolVersion: 1, minProtocolVersion: 1} as const;

export const methodPathOf = (method: string): string => `${PREFIX}${method}`;

/** The one call of the host's control service that a session makes. */
const IDENTIFY = `/${HostControlService.typeName}/${HostControlService.method.identify.name}`;

const lost = (message: string): Step => ({end: {message, status: RpcStatus.UNAVAILABLE}});

/** The sequence each stream asks to continue after, read from its request, by method. */
const CURSORS: Readonly<Record<string, (request: Uint8Array) => bigint>> = {
  [methodPathOf('WatchRuns')]: request => fromBinary(WatchRunsRequestSchema, request).afterSequence,
  [methodPathOf('WatchAgent')]: request =>
    fromBinary(WatchAgentRequestSchema, request).afterSequence,
};

/**
 * What the host answers a stream asked to continue after a sequence its log has not reached, as
 * the daemon's `ErrAheadOfLog` does; undefined for a call with no cursor or one the log holds.
 */
function aheadOfLog(daemon: FakeDaemon, target: CallTarget): ReadonlyArray<Step> | undefined {
  const after = CURSORS[target.method]?.(target.request) ?? 0n;
  if (after <= daemon.sequence) return undefined;
  const message = `daemon: that sequence is ahead of the log; start again from a snapshot (asked after ${after}, the log is at ${daemon.sequence})`;
  return [{end: {message, status: RpcStatus.OUT_OF_RANGE}}];
}

/** The per-agent frame an event becomes for `agentId`, or null when it is not about that agent. */
function agentFrameOf(
  event: RunChange,
  agentId: string,
): Parameters<typeof create<typeof WatchAgentResponseSchema>>[1] | null {
  const {body} = event;
  if (
    oneofIs(body, 'item', {
      run: true,
      agent: true,
      turn: true,
      item: true,
      message: true,
      receipt: true,
      checkIn: true,
      runUpdated: true,
    }) &&
    body.value.agentId === agentId
  ) {
    return {sequence: event.sequence, body: {case: 'item', value: body.value}};
  }
  if (
    oneofIs(body, 'turn', {
      run: true,
      agent: true,
      turn: true,
      item: true,
      message: true,
      receipt: true,
      checkIn: true,
      runUpdated: true,
    }) &&
    body.value.agentId === agentId
  ) {
    return {sequence: event.sequence, body: {case: 'turn', value: body.value}};
  }
  return null;
}

/** The page of older items a ListItems call asks for. */
function listItemsSteps(daemon: FakeDaemon, target: CallTarget): ReadonlyArray<Step> {
  const request = fromBinary(ListItemsRequestSchema, target.request);
  const page = daemon.page(request.agentId, request.pageSize, request.beforeOrdinal);
  const bytes = toBinary(
    ListItemsResponseSchema,
    create(ListItemsResponseSchema, {page: create(ItemsPageSchema, page)}),
  );
  return [{frame: bytes}];
}

/** The whole item a `GetItem` call asks for, or NotFound. */
function getItemSteps(daemon: FakeDaemon, target: CallTarget): ReadonlyArray<Step> {
  const request = fromBinary(GetItemRequestSchema, target.request);
  const item = daemon.item(request.agentId, request.ordinal);
  if (item === undefined) return [{end: {message: 'no such item', status: RpcStatus.NOT_FOUND}}];
  const bytes = toBinary(
    GetItemResponseSchema,
    create(GetItemResponseSchema, {item, sequence: daemon.sequence}),
  );
  return [{frame: bytes}];
}

/** The one view of a run's cost the host would send when a stream opens, and then the stream. */
function costSteps(
  daemon: FakeDaemon,
  target: CallTarget,
  tail: ReadonlyArray<Step>,
): ReadonlyArray<Step> {
  const {runId} = fromBinary(WatchRunCostRequestSchema, target.request);
  const cost = daemon.costOf(runId);
  if (cost === undefined) {
    return [{end: {message: `no run ${runId}`, status: RpcStatus.NOT_FOUND}}];
  }
  const frame = toBinary(WatchRunCostResponseSchema, create(WatchRunCostResponseSchema, {cost}));
  return [{frame}, ...tail];
}

/** The one view of the host's cost the host would send when a stream opens, and then the stream. */
function hostCostSteps(
  daemon: FakeDaemon,
  target: CallTarget,
  tail: ReadonlyArray<Step>,
): ReadonlyArray<Step> {
  const {since} = fromBinary(WatchHostCostRequestSchema, target.request);
  const cost = daemon.hostCostOf(since === undefined ? null : timestampMs(since));
  if (cost === undefined) {
    return [{end: {message: 'no host cost view for this window', status: RpcStatus.NOT_FOUND}}];
  }
  const frame = toBinary(WatchHostCostResponseSchema, create(WatchHostCostResponseSchema, {cost}));
  return [{frame}, ...tail];
}

/** The whole page of a run's history a `GetRunHistory` call asks for, or NotFound for a run the host lacks. */
function runHistorySteps(
  daemon: FakeDaemon,
  target: CallTarget,
  pageMessages: number | undefined,
): ReadonlyArray<Step> {
  const request = fromBinary(GetRunHistoryRequestSchema, target.request);
  if (!daemon.summaries().some(summary => summary.runId === request.runId))
    return [{end: {message: `no run ${request.runId}`, status: RpcStatus.NOT_FOUND}}];
  const page = daemon.history(request.runId, request.cursor, pageMessages);
  return [{frame: toBinary(GetRunHistoryResponseSchema, page)}];
}

const frameOf = (body: Parameters<typeof create<typeof WatchRunsResponseSchema>>[1]): Uint8Array =>
  toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, body));

/** The summaries that differ from the ones in `held`, which then holds them: what the host would send. */
function changedSummaries(
  now: readonly RunSummary[],
  held: Map<string, Uint8Array>,
): ReadonlyArray<RunSummary> {
  return now.filter(summary => {
    const bytes = toBinary(RunSummarySchema, summary);
    const before = held.get(summary.runId);
    if (before !== undefined && equalBytes(before, bytes)) return false;
    held.set(summary.runId, bytes);
    return true;
  });
}

const equalBytes = (a: Uint8Array, b: Uint8Array): boolean =>
  a.length === b.length && a.every((byte, i) => byte === b[i]);

/**
 * A step that carries each change the daemon commits from now on, as it commits it, in the log's
 * order, and, for a stream that asked for summaries, the summaries each batch of changes moved.
 */
function followRuns(daemon: FakeDaemon, summaries: Map<string, Uint8Array> | null): Step {
  let sent = daemon.sequence;
  return {
    follow: emit =>
      daemon.watch(() => {
        for (const event of daemon.changesAfter(sent)) {
          sent = event.sequence;
          emit(frameOf({body: {case: 'change', value: event}}));
        }
        if (summaries !== null)
          for (const summary of changedSummaries(daemon.summaries(), summaries))
            emit(frameOf({body: {case: 'summary', value: summary}}));
      }),
  };
}

/**
 * What a WatchRuns call does: the snapshot when it starts from none, the changes after its cursor, the
 * search views and summaries it has not been sent, and then the stream. A stream of detail SUMMARY is the
 * one that is sent summary frames; it holds the summaries it sent.
 */
function runsSteps(
  daemon: FakeDaemon,
  target: CallTarget,
  live: boolean,
  tail: ReadonlyArray<Step>,
): ReadonlyArray<Step> {
  const {afterSequence, detail} = fromBinary(WatchRunsRequestSchema, target.request);
  const encode = (body: Parameters<typeof create<typeof WatchRunsResponseSchema>>[1]): Step => ({
    frame: frameOf(body),
  });
  let after = afterSequence;
  const steps: Step[] = [];
  const seen = new Map<string, bigint>();
  const summaries = detail === SnapshotDetail.SUMMARY ? new Map<string, Uint8Array>() : null;
  // The host as it stands now: what the snapshot says, and what has not been sent beside it.
  const now = daemon.snapshot(detail);
  if (after === 0n) {
    after = now.sequence;
    steps.push(encode({body: {case: 'snapshot', value: now}}));
    for (const search of now.searches) seen.set(search.runId, search.revision);
    if (summaries !== null) changedSummaries(now.summaries, summaries);
  }
  for (const event of daemon.changesAfter(after)) {
    steps.push(encode({body: {case: 'change', value: event}}));
  }
  for (const search of now.searches) {
    if (search.revision > (seen.get(search.runId) ?? 0n))
      steps.push(encode({body: {case: 'search', value: search}}));
  }
  if (summaries !== null)
    for (const summary of changedSummaries(now.summaries, summaries))
      steps.push(encode({body: {case: 'summary', value: summary}}));
  return [...steps, ...(live ? [followRuns(daemon, summaries)] : tail)];
}

/** What a `CheckRepository` call answers: the host's refusal of the path, or none, and its tasks. */
function checkRepositorySteps(options: DaemonOptions, target: CallTarget): ReadonlyArray<Step> {
  const {repository} = fromBinary(CheckRepositoryRequestSchema, target.request);
  const check = options.checkRepository;
  if (check === 'unserved')
    return [{end: {message: 'unknown method', status: RpcStatus.UNIMPLEMENTED}}];
  const refusal =
    check !== undefined
      ? invokeCallback(() => check(repository))
      : refusalOf(options.files?.refusal(repository));
  const answer =
    refusal === undefined
      ? {tasks: [...(options.files?.tasksOf(repository) ?? [])]}
      : {refusal: {reason: refusal.reason, notReady: refusal.notReady === true}};
  return [
    {
      frame: toBinary(CheckRepositoryResponseSchema, create(CheckRepositoryResponseSchema, answer)),
    },
  ];
}

const refusalOf = (reason: string | undefined) =>
  reason === undefined ? undefined : {reason, notReady: false};

/** What a `ListDirectory` call answers: the folder's entries, or the fault of listing it. */
function listDirectorySteps(options: DaemonOptions, target: CallTarget): ReadonlyArray<Step> {
  const {files} = options;
  if (files === undefined)
    return [{end: {message: 'unknown method', status: RpcStatus.UNIMPLEMENTED}}];
  const {path, includeHidden} = fromBinary(ListDirectoryRequestSchema, target.request);
  const listed = files.list(path, includeHidden);
  if (!('entries' in listed)) return [{end: {message: listed.message, status: listed.status}}];
  return [
    {
      frame: toBinary(
        ListDirectoryResponseSchema,
        create(ListDirectoryResponseSchema, {
          entries: listed.entries.map(entry => ({...entry})),
          truncated: listed.truncated,
        }),
      ),
    },
  ];
}

/** What a `WatchAgent` call answers: the agent's first page, its scenario deltas, then changes, then the tail. */
function watchAgentSteps(
  daemon: FakeDaemon,
  target: CallTarget,
  tail: ReadonlyArray<Step>,
): ReadonlyArray<Step> {
  const request = fromBinary(WatchAgentRequestSchema, target.request);
  const encode = (init: Parameters<typeof create<typeof WatchAgentResponseSchema>>[1]): Step => ({
    frame: toBinary(WatchAgentResponseSchema, create(WatchAgentResponseSchema, init)),
  });
  const steps: Step[] = [];
  let after = request.afterSequence;
  if (after === 0n) {
    const page = daemon.page(request.agentId, request.pageSize, 0);
    after = page.sequence;
    steps.push(encode({sequence: page.sequence, body: {case: 'page', value: page}}));
    for (const delta of daemon.scenario.deltas) {
      if (delta.agentId === request.agentId) {
        steps.push(encode({sequence: page.sequence, body: {case: 'delta', value: delta}}));
      }
    }
  }
  for (const event of daemon.changesAfter(after)) {
    const init = agentFrameOf(event, request.agentId);
    if (init !== null) steps.push(encode(init));
  }
  return [...steps, ...tail];
}

export function daemonRecording(daemon: FakeDaemon, options: DaemonOptions = {}): Recording {
  const tail: ReadonlyArray<Step> =
    options.closeStreams === true ? [{close: true}] : [{hang: true}];

  const hello = (nth: number): ReadonlyArray<Step> => {
    const failure = invokeCallback(() => options.helloFails?.(nth));
    if (failure !== undefined) return [{end: {message: 'hello failed', status: failure}}];
    const bytes = toBinary(
      HelloResponseSchema,
      create(HelloResponseSchema, {
        daemon: create(BuildVersionsSchema, FAKE_DAEMON),
        compatibility: options.compatibility ?? Compatibility.COMPATIBLE,
      }),
    );
    return [{frame: bytes}];
  };

  const dispatch = (target: CallTarget, nth: number): ReadonlyArray<Step> => {
    const fate = invokeCallback(() => options.dispatch?.(nth)) ?? 'ok';
    if (fate === 'lose-request') return [lost('connection lost before the command arrived')];
    const {command} = fromBinary(DispatchRequestSchema, target.request);
    if (command === undefined) return [{end: {status: RpcStatus.INVALID_ARGUMENT}}];
    const refused = daemon.callRefusal(command);
    if (refused !== undefined)
      return [{end: {message: refused, status: RpcStatus.INVALID_ARGUMENT}}];
    const receipt = daemon.dispatch(command);
    if (fate === 'lose-reply') return [lost('connection lost before the reply arrived')];
    const bytes = toBinary(DispatchResponseSchema, create(DispatchResponseSchema, {receipt}));
    return [{frame: bytes}];
  };

  const newRun = newRunSteps(new Set<string>());

  return {
    respond: (target, nth) => {
      const ahead = aheadOfLog(daemon, target);
      if (ahead !== undefined) return ahead;
      switch (target.method) {
        case methodPathOf('Hello'):
          return hello(nth);
        case methodPathOf('Dispatch'):
          return dispatch(target, nth);
        case IDENTIFY:
          return options.identify === undefined
            ? [{end: {message: 'no host control', status: RpcStatus.UNIMPLEMENTED}}]
            : [{frame: toBinary(IdentifyResponseSchema, options.identify())}];
        case methodPathOf('ListItems'):
          return listItemsSteps(daemon, target);
        case methodPathOf('GetItem'):
          return getItemSteps(daemon, target);
        case methodPathOf('GetRunHistory'):
          return runHistorySteps(daemon, target, options.historyPageMessages);
        case methodPathOf('CheckRepository'):
          return checkRepositorySteps(options, target);
        case methodPathOf('ListCatalogue'):
          return options.catalogue === undefined
            ? newRun('ListCatalogue', target.request)
            : [{frame: toBinary(ListCatalogueResponseSchema, options.catalogue)}];
        case methodPathOf('ListEvalPlaces'):
        case methodPathOf('ProvisionRepository'):
          return newRun(target.method.slice(PREFIX.length), target.request);
        case methodPathOf('ListDirectory'):
          return listDirectorySteps(options, target);
        case methodPathOf('WatchRuns'):
          return runsSteps(daemon, target, options.live === true, tail);
        case methodPathOf('WatchAgent'):
          return watchAgentSteps(daemon, target, tail);
        case methodPathOf('WatchRunCost'):
          return costSteps(daemon, target, tail);
        case methodPathOf('WatchHostCost'):
          return hostCostSteps(daemon, target, tail);
        default:
          return [
            {end: {message: `unknown method ${target.method}`, status: RpcStatus.UNIMPLEMENTED}},
          ];
      }
    },
  };
}
