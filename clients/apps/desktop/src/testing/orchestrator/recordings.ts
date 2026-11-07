/**
 * A `Recording` of the orchestrator service played by a `FakeDaemon`: the same answers the daemon
 * would give, as encoded frames, so everything above the transport runs for real. Faults of the
 * link are `withFaults` of `testing/transport`; faults only the daemon can cause (a command it
 * applied but whose reply was lost) are `DaemonOptions`.
 */
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {
  BuildVersionsSchema,
  Compatibility,
  DispatchRequestSchema,
  DispatchResponseSchema,
  HelloResponseSchema,
  ItemsPageSchema,
  ListItemsRequestSchema,
  ListItemsResponseSchema,
  OrchestratorService,
  oneofIs,
  type RunChange,
  WatchAgentRequestSchema,
  WatchAgentResponseSchema,
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
}

const PREFIX = `/${OrchestratorService.typeName}/`;

/** The fixed protocol revision the fake daemon speaks. */
const FAKE_DAEMON = {binaryVersion: 'fake', protocolVersion: 1, minProtocolVersion: 1} as const;

export const methodPathOf = (method: string): string => `${PREFIX}${method}`;

const lost = (message: string): Step => ({end: {message, status: RpcStatus.UNAVAILABLE}});

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
    const receipt = daemon.dispatch(command);
    if (fate === 'lose-reply') return [lost('connection lost before the reply arrived')];
    const bytes = toBinary(DispatchResponseSchema, create(DispatchResponseSchema, {receipt}));
    return [{frame: bytes}];
  };

  const watchRuns = (target: CallTarget): ReadonlyArray<Step> => {
    const {afterSequence} = fromBinary(WatchRunsRequestSchema, target.request);
    const encode = (body: Parameters<typeof create<typeof WatchRunsResponseSchema>>[1]): Step => ({
      frame: toBinary(WatchRunsResponseSchema, create(WatchRunsResponseSchema, body)),
    });
    let after = afterSequence;
    const steps: Step[] = [];
    const seen = new Map<string, bigint>();
    if (after === 0n) {
      const snapshot = daemon.snapshot();
      after = snapshot.sequence;
      steps.push(encode({body: {case: 'snapshot', value: snapshot}}));
      for (const search of snapshot.searches) seen.set(search.runId, search.revision);
    }
    for (const event of daemon.changesAfter(after)) {
      steps.push(encode({body: {case: 'change', value: event}}));
    }
    for (const search of daemon.snapshot().searches) {
      if (search.revision > (seen.get(search.runId) ?? 0n))
        steps.push(encode({body: {case: 'search', value: search}}));
    }
    return [...steps, ...tail];
  };

  const watchAgent = (target: CallTarget): ReadonlyArray<Step> => {
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
  };

  return {
    respond: (target, nth) => {
      switch (target.method) {
        case methodPathOf('Hello'):
          return hello(nth);
        case methodPathOf('Dispatch'):
          return dispatch(target, nth);
        case methodPathOf('ListItems'):
          return listItemsSteps(daemon, target);
        case methodPathOf('WatchRuns'):
          return watchRuns(target);
        case methodPathOf('WatchAgent'):
          return watchAgent(target);
        case methodPathOf('WatchRunCost'):
          return costSteps(daemon, target, tail);
        default:
          return [
            {end: {message: `unknown method ${target.method}`, status: RpcStatus.UNIMPLEMENTED}},
          ];
      }
    },
  };
}
