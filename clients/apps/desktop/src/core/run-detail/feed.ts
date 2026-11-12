/**
 * One mounted run's generic detail: acquire a committed Skeleton, then follow Tail from its
 * `next_seq`. Every restart begins with another Skeleton, so a reconnect or rejected cursor can
 * never join two epochs of the log by assumption. The pure run-detail reducer remains the only
 * owner of sequence validation and projections.
 */
import {type HostService, SkeletonResponseSchema, type TailResponse} from '@vibesys/proto';
import type * as Context from 'effect/Context';
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import type {Fault} from '../../state/index.js';
import {
  emptyRunDetail,
  ingestRunDetailSkeleton,
  ingestRunDetailTail,
  projectRunDetail,
  type RunDetailProjection,
  type RunDetailState,
  type RunDetailUpdate,
} from '../../state/run-detail/index.js';
import type {EffectClient} from '../client/make-client.js';
import type {HostConnectionApi} from '../connection.js';
import {type AppError, InvalidMessage, isAheadOfLog, isRetryable, Unavailable} from '../errors.js';
import {faultOf} from '../fault.js';
import {MAX_QUIET_RESTARTS, STABLE_PASS_FRAMES} from '../policy.js';
import {makeRetryGate, type RetryGate} from '../retry-gate.js';

type HostClient = EffectClient<typeof HostService.method>;

type RunDetailStaleReason =
  | 'catching-up'
  | 'refreshing'
  | 'gap'
  | 'invalid-data'
  | 'fell-behind'
  | 'ahead-of-log'
  | 'closed'
  | 'disconnected';

/** What a consumer knows about delivery, apart from freshness inside the projection itself. */
export type RunDetailDelivery =
  | {readonly _tag: 'Loading'}
  | {readonly _tag: 'Live'; readonly projection: RunDetailProjection}
  | {
      readonly _tag: 'Stale';
      readonly why: RunDetailStaleReason;
      readonly projection: RunDetailProjection | null;
    }
  | {
      readonly _tag: 'Refused';
      readonly fault: Fault;
      readonly projection: RunDetailProjection | null;
    }
  | {
      readonly _tag: 'Failed';
      readonly fault: Fault;
      readonly projection: RunDetailProjection | null;
    };

export interface RunDetailFeed {
  /** Follow `runId` while mounted. Two consumers of the same atom share one lifetime. */
  readonly watch: (runId: string) => Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  /** Delivery and the latest safe typed projection for `runId`. */
  readonly state: (runId: string) => Atom.Atom<RunDetailDelivery>;
  /** Retry a mounted run that stopped on a refusal or invalid contract. */
  readonly retry: (runId: string) => Effect.Effect<void>;
}

interface RunDetailParts {
  readonly client: HostClient;
  readonly connection: HostConnectionApi;
  readonly ambient: Context.Context<never>;
}

type Mode =
  | {readonly _tag: 'Loading'}
  | {readonly _tag: 'Live'}
  | {readonly _tag: 'Stale'; readonly why: RunDetailStaleReason}
  | {readonly _tag: 'Refused'; readonly fault: Fault}
  | {readonly _tag: 'Failed'; readonly fault: Fault};

interface Held {
  readonly detail: RunDetailState;
  readonly mode: Mode;
}

type HeldByRun = ReadonlyMap<string, Held>;

type PassEnd =
  | {
      readonly _tag: 'Restart';
      readonly why: RunDetailStaleReason;
      readonly progressed: boolean;
      /** A repeated broken reducer input eventually stops instead of redialing forever. */
      readonly invalid?: InvalidMessage;
    }
  | {readonly _tag: 'Lost'; readonly error: AppError}
  | {readonly _tag: 'Refused'; readonly error: AppError}
  | {readonly _tag: 'Failed'; readonly error: AppError};

interface Next {
  readonly after: number;
  readonly quiet: number;
}

interface Wiring extends RunDetailParts {
  readonly held: SubscriptionRef.SubscriptionRef<HeldByRun>;
  readonly gates: Map<string, RetryGate>;
}

type SkeletonTurn = PassEnd | {readonly _tag: 'Ready'; readonly nextSequence: bigint};

const initial = (runId: string): Held => ({detail: emptyRunDetail(runId), mode: {_tag: 'Loading'}});

const projectionOf = (held: Held): RunDetailProjection | null =>
  held.detail.cursor.kind === 'empty' ? null : projectRunDetail(held.detail);

const deliveryOf = (held: Held): RunDetailDelivery => {
  const projection = projectionOf(held);
  switch (held.mode._tag) {
    case 'Loading':
      return {_tag: 'Loading'};
    case 'Live':
      return {_tag: 'Live', projection: projectRunDetail(held.detail)};
    case 'Stale':
      return {_tag: 'Stale', why: held.mode.why, projection};
    case 'Refused':
      return {_tag: 'Refused', fault: held.mode.fault, projection};
    case 'Failed':
      return {_tag: 'Failed', fault: held.mode.fault, projection};
  }
};

const modeOf = (detail: RunDetailState): Mode => {
  const freshness = projectRunDetail(detail).freshness;
  return freshness.kind === 'current' ? {_tag: 'Live'} : {_tag: 'Stale', why: 'catching-up'};
};

const invalidResponse = (detail: string): InvalidMessage =>
  new InvalidMessage({
    direction: 'response',
    type: SkeletonResponseSchema.typeName,
    detail,
  });

const restartFromUpdate = (
  update: Extract<RunDetailUpdate, {readonly kind: 'refresh-required'}>,
  progressed: boolean,
): PassEnd => ({
  _tag: 'Restart',
  why: update.reason === 'gap' ? 'gap' : 'invalid-data',
  progressed,
  ...(update.reason === 'gap' ? {} : {invalid: invalidResponse(`tail: ${update.reason}`)}),
});

/** What a call failure means to this detail lifetime. */
function failedPass(error: AppError, phase: 'skeleton' | 'tail', progressed: boolean): PassEnd {
  if (phase === 'tail' && error._tag === 'FellBehind') {
    return {_tag: 'Restart', why: 'fell-behind', progressed};
  }
  if (phase === 'tail' && isAheadOfLog(error)) {
    return {_tag: 'Restart', why: 'ahead-of-log', progressed};
  }
  if (isRetryable(error) || error._tag === 'NoBridge') return {_tag: 'Lost', error};
  if (error._tag === 'InvalidMessage') return {_tag: 'Failed', error};
  return {_tag: 'Refused', error};
}

const getHeld = (w: Wiring, runId: string) =>
  SubscriptionRef.get(w.held).pipe(Effect.map(all => all.get(runId) ?? initial(runId)));

const setHeld = (w: Wiring, runId: string, value: Held) =>
  SubscriptionRef.update(w.held, all => new Map(all).set(runId, value));

const modifyHeld = <A>(w: Wiring, runId: string, change: (current: Held) => readonly [A, Held]) =>
  SubscriptionRef.modify(w.held, all => {
    const [value, next] = change(all.get(runId) ?? initial(runId));
    return [value, new Map(all).set(runId, next)];
  });

const setMode = (w: Wiring, runId: string, mode: Mode) =>
  modifyHeld(w, runId, current => [undefined, {...current, mode}] as const).pipe(Effect.asVoid);

const heldAfterSkeleton = (update: RunDetailUpdate): Held => {
  switch (update.kind) {
    case 'applied':
      return {detail: update.state, mode: modeOf(update.state)};
    case 'duplicate':
    case 'refresh-required':
    case 'rejected-snapshot':
      return {detail: update.state, mode: {_tag: 'Stale', why: 'invalid-data'}};
  }
};

const skeletonFailure = (update: RunDetailUpdate): string => {
  switch (update.kind) {
    case 'applied':
      return 'skeleton: reducer unexpectedly accepted the rejected result';
    case 'duplicate':
      return 'skeleton: reducer returned duplicate';
    case 'refresh-required':
      return `skeleton: ${update.reason}`;
    case 'rejected-snapshot':
      return update.reason;
  }
};

const acquireSkeleton = (w: Wiring, runId: string): Effect.Effect<SkeletonTurn> =>
  Effect.gen(function* () {
    const before = yield* getHeld(w, runId);
    const result = yield* w.client
      .skeleton({
        runId,
        knownDescriptorsSha256: before.detail.descriptors.map(item => item.sha256),
      })
      .pipe(Effect.result);
    if (result._tag === 'Failure') return failedPass(result.failure, 'skeleton', false);
    yield* w.connection.heard;
    const update = yield* modifyHeld(w, runId, current => {
      const next = ingestRunDetailSkeleton(current.detail, result.success);
      return [next, heldAfterSkeleton(next)] as const;
    });
    if (update.kind !== 'applied') {
      return {
        _tag: 'Restart',
        why: 'invalid-data',
        progressed: false,
        invalid: invalidResponse(skeletonFailure(update)),
      };
    }
    return update.state.cursor.kind === 'ready'
      ? {_tag: 'Ready', nextSequence: update.state.cursor.nextSequence}
      : {
          _tag: 'Restart',
          why: 'invalid-data',
          progressed: false,
          invalid: invalidResponse('skeleton: did not establish a ready cursor'),
        };
  });

const heldAfterTail = (current: Held, update: RunDetailUpdate): Held => {
  switch (update.kind) {
    case 'applied':
      return {detail: update.state, mode: modeOf(update.state)};
    case 'duplicate':
      return current;
    case 'refresh-required':
      return {
        detail: update.state,
        mode: {_tag: 'Stale', why: update.reason === 'gap' ? 'gap' : 'invalid-data'},
      };
    case 'rejected-snapshot':
      return {detail: update.state, mode: {_tag: 'Stale', why: 'invalid-data'}};
  }
};

const ingestTail = (w: Wiring, runId: string, response: TailResponse) =>
  Effect.gen(function* () {
    yield* w.connection.heard;
    return yield* modifyHeld(w, runId, current => {
      const update = ingestRunDetailTail(current.detail, response);
      return [update, heldAfterTail(current, update)] as const;
    });
  });

const restartAfterTail = (update: RunDetailUpdate, progressed: boolean): PassEnd | null => {
  switch (update.kind) {
    case 'applied':
    case 'duplicate':
      return null;
    case 'refresh-required':
      return restartFromUpdate(update, progressed);
    case 'rejected-snapshot':
      return {
        _tag: 'Restart',
        why: 'invalid-data',
        progressed,
        invalid: invalidResponse(update.reason),
      };
  }
};

const followTail = (w: Wiring, runId: string, nextSequence: bigint): Effect.Effect<PassEnd> =>
  Effect.gen(function* () {
    let inOrder = 0;
    let restart: PassEnd | null = null;
    const result = yield* w.client.tail({runId, fromSeq: nextSequence, sessionId: ''}).pipe(
      Stream.mapEffect(response =>
        Effect.gen(function* () {
          const update = yield* ingestTail(w, runId, response);
          if (update.kind === 'applied') inOrder += 1;
          restart = restartAfterTail(update, inOrder >= STABLE_PASS_FRAMES);
          return restart === null;
        }),
      ),
      Stream.takeWhile(keep => keep),
      Stream.runDrain,
      Effect.result,
    );
    const progressed = inOrder >= STABLE_PASS_FRAMES;
    if (restart !== null) return restart;
    if (result._tag === 'Failure') return failedPass(result.failure, 'tail', progressed);
    return {_tag: 'Restart', why: 'closed', progressed};
  });

/** Open one Skeleton/Tail pair and stop at the first reason it must be reacquired. */
const pass = (w: Wiring, runId: string): Effect.Effect<PassEnd> =>
  Effect.gen(function* () {
    const acquired = yield* acquireSkeleton(w, runId);
    return acquired._tag === 'Ready'
      ? yield* followTail(w, runId, acquired.nextSequence)
      : acquired;
  });

const afterRestart = (
  w: Wiring,
  runId: string,
  gate: RetryGate,
  epoch: number,
  prior: Next,
  ended: Extract<PassEnd, {readonly _tag: 'Restart'}>,
): Effect.Effect<Next> =>
  Effect.gen(function* () {
    yield* setMode(w, runId, {_tag: 'Stale', why: ended.why});
    const quiet = ended.progressed ? 0 : prior.quiet + 1;
    if (quiet <= MAX_QUIET_RESTARTS) return {after: epoch - 1, quiet};
    if (ended.invalid !== undefined) {
      yield* gate.wait(setMode(w, runId, {_tag: 'Failed', fault: faultOf(ended.invalid)}));
      return {after: epoch - 1, quiet: 0};
    }
    const stuck = new Unavailable({detail: 'the run detail keeps ending before progress'});
    yield* setMode(w, runId, {_tag: 'Stale', why: 'disconnected'});
    yield* w.connection.suspect(epoch, stuck);
    return {after: epoch, quiet: 0};
  });

const afterPass = (
  w: Wiring,
  runId: string,
  gate: RetryGate,
  epoch: number,
  prior: Next,
  ended: PassEnd,
): Effect.Effect<Next> => {
  switch (ended._tag) {
    case 'Lost':
      return setMode(w, runId, {_tag: 'Stale', why: 'disconnected'}).pipe(
        Effect.andThen(w.connection.suspect(epoch, ended.error)),
        Effect.as({after: epoch, quiet: 0}),
      );
    case 'Refused':
      return gate
        .wait(setMode(w, runId, {_tag: 'Refused', fault: faultOf(ended.error)}))
        .pipe(Effect.as({after: epoch - 1, quiet: 0}));
    case 'Failed':
      return gate
        .wait(setMode(w, runId, {_tag: 'Failed', fault: faultOf(ended.error)}))
        .pipe(Effect.as({after: epoch - 1, quiet: 0}));
    case 'Restart':
      return afterRestart(w, runId, gate, epoch, prior, ended);
  }
};

const follow = (w: Wiring, runId: string, gate: RetryGate): Effect.Effect<never> =>
  Effect.gen(function* () {
    let next: Next = {after: -1, quiet: 0};
    while (true) {
      const ready = yield* w.connection.ready(next.after).pipe(Effect.result);
      if (ready._tag === 'Failure') {
        yield* w.connection.leftTerminal;
        continue;
      }
      const current = yield* getHeld(w, runId);
      const mode: Mode =
        projectionOf(current) === null ? {_tag: 'Loading'} : {_tag: 'Stale', why: 'refreshing'};
      yield* setMode(w, runId, mode);
      const ended = yield* pass(w, runId);
      next = yield* afterPass(w, runId, gate, ready.success, next, ended);
    }
  });

const forget = (w: Wiring, runId: string) =>
  Effect.suspend(() => {
    w.gates.delete(runId);
    return SubscriptionRef.update(w.held, all => {
      const rest = new Map(all);
      rest.delete(runId);
      return rest;
    });
  });

const watchRun = (w: Wiring, runId: string) =>
  Effect.gen(function* () {
    const gate = yield* makeRetryGate;
    w.gates.set(runId, gate);
    yield* setHeld(w, runId, initial(runId));
    return yield* follow(w, runId, gate);
  }).pipe(Effect.provideContext(w.ambient), Effect.ensuring(forget(w, runId)));

/** Construct one per-host generic detail service. It opens no call until a run is mounted. */
export const makeRunDetailFeed = Effect.fn('RunDetailFeed.make')(function* (parts: RunDetailParts) {
  const held = yield* SubscriptionRef.make<HeldByRun>(new Map());
  const heldAtom = Atom.keepAlive(Atom.subscriptionRef(held));
  const wiring: Wiring = {...parts, held, gates: new Map()};
  const state = Atom.family((runId: string) =>
    Atom.make(getAtom => deliveryOf(getAtom(heldAtom).get(runId) ?? initial(runId))),
  );
  return {
    watch: Atom.family((runId: string) => Atom.make(watchRun(wiring, runId))),
    state,
    retry: runId => wiring.gates.get(runId)?.open ?? Effect.void,
  } satisfies RunDetailFeed;
});
