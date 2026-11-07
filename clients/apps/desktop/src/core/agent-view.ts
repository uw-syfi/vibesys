/**
 * One agent's stream: its newest items, then its live progress, while a screen watches it. The
 * cursor of an agent's stream is valid only within one mount: the first open of a mount reads the
 * newest page, a reopen while mounted resumes after what was applied, unless the host's log no longer
 * reaches it (then the reopen reads the newest page again).
 */
import * as Effect from 'effect/Effect';
import type * as AsyncResult from 'effect/reactivity/AsyncResult';
import * as Atom from 'effect/reactivity/Atom';
import * as Stream from 'effect/Stream';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {rewindAgentStream} from '../state/host-model.js';
import {
  applyHostFrame,
  type FeedStatus,
  type Freshness,
  freshnessOf,
  type HostFrame,
} from '../state/index.js';
import {type FeedSpec, runFeed} from './feeds.js';
import type {HostParts} from './host-store.js';
import {AGENT_PAGE_SIZE} from './policy.js';
import {makeRetryGate, type RetryGate} from './retry-gate.js';

export interface AgentView {
  /** Mount this to follow one agent; unmounting closes its stream. Mounting twice shares one. */
  readonly watch: (agentId: string) => Atom.Atom<AsyncResult.AsyncResult<never, never>>;
  readonly status: (agentId: string) => Atom.Atom<FeedStatus>;
  readonly freshness: (agentId: string) => Atom.Atom<Freshness>;
  /** The user asked to retry this agent's failed stream. */
  readonly retry: (agentId: string) => Effect.Effect<void>;
}

type Statuses = Readonly<Record<string, FeedStatus>>;

const OPENING: FeedStatus = {_tag: 'Opening'};

const agentSpec = ({client}: HostParts, agentId: string): FeedSpec<HostFrame> => ({
  open: (model, firstOpen) =>
    client
      .watchAgent({
        agentId,
        pageSize: AGENT_PAGE_SIZE,
        afterSequence: firstOpen ? 0n : (model.agentCursors[agentId] ?? 0n),
      })
      .pipe(Stream.map(frame => ({from: 'agent', agentId, frame}) as const)),
  apply: applyHostFrame,
  gapped: () => false,
  rewind: model => rewindAgentStream(model, agentId),
});

export const makeAgentView = Effect.fn('AgentView.make')(function* (parts: HostParts) {
  const {connection, store} = parts;
  const statuses = yield* SubscriptionRef.make<Statuses>({});
  const statusesAtom = Atom.keepAlive(Atom.subscriptionRef(statuses));
  /** The gate of each mounted agent's feed: there while it is mounted, gone when it unmounts. */
  const gates = new Map<string, RetryGate>();
  const forgetAgent = (agentId: string) =>
    Effect.suspend(() => {
      gates.delete(agentId);
      return SubscriptionRef.update(statuses, ({[agentId]: _gone, ...rest}) => rest);
    });

  const status = Atom.family((agentId: string) =>
    Atom.make(get => get(statusesAtom)[agentId] ?? OPENING),
  );
  const watch = Atom.family((agentId: string) =>
    Atom.make(
      Effect.gen(function* () {
        const gate = yield* makeRetryGate;
        gates.set(agentId, gate);
        return yield* runFeed(
          {
            connection,
            model: store.ref,
            setStatus: next => SubscriptionRef.update(statuses, all => ({...all, [agentId]: next})),
            awaitRetry: gate.wait,
          },
          agentSpec(parts, agentId),
        );
      }).pipe(
        Effect.provideContext(parts.ambient),
        // Unmounting ends the feed: its status and gate go with it.
        Effect.ensuring(forgetAgent(agentId)),
      ),
    ),
  );
  const view: AgentView = {
    watch,
    status,
    freshness: Atom.family((agentId: string) =>
      Atom.make(get =>
        freshnessOf(
          get(connection.state),
          get(status(agentId)),
          get(store.atom).agentCursors[agentId] !== undefined,
        ),
      ),
    ),
    // Only a mounted feed has a gate; a retry for any other agent has nothing to release.
    retry: agentId => gates.get(agentId)?.open ?? Effect.void,
  };
  return view;
});
