/**
 * Reads of one agent's history that the live stream does not carry: the page of items before the
 * oldest one loaded, and the whole of an item a page left out the large text of. Each is one
 * request, never a stream; its answer folds into the same model the streams fold into, and the
 * agent stream's cursor does not move (an old page says nothing about what the stream has yet to
 * deliver). A read that is already under way is not made again, and a failed one says so as the
 * status of the thing it was reading.
 */
import * as Effect from 'effect/Effect';
import * as Atom from 'effect/reactivity/Atom';
import * as SubscriptionRef from 'effect/SubscriptionRef';
import {applyHostFrame, type Fault, type HostFrame} from '../state/index.js';
import type {AppError} from './errors.js';
import {faultOf} from './fault.js';
import type {HostParts} from './host-store.js';
import {AGENT_PAGE_SIZE} from './policy.js';

/** Where reading earlier items, or one item whole, stands. */
export type HistoryStatus =
  | {readonly _tag: 'Idle'}
  | {readonly _tag: 'Loading'}
  | {readonly _tag: 'Failed'; readonly fault: Fault};

export interface AgentHistory {
  /**
   * Reads the page of items of `agentId` below ordinal `before`. Does nothing while a read of the
   * same agent is under way. Never fails: a failure is the status.
   */
  readonly loadEarlier: (agentId: string, before: number) => Effect.Effect<void>;
  /** Reads the item at `ordinal` of `agentId` whole. Never fails: a failure is the status. */
  readonly expand: (agentId: string, ordinal: number) => Effect.Effect<void>;
  /** Where reading earlier items of the agent stands. */
  readonly earlier: (agentId: string) => Atom.Atom<HistoryStatus>;
  /** Where reading the item at `ordinal` of the agent whole stands. */
  readonly whole: (agentId: string, ordinal: number) => Atom.Atom<HistoryStatus>;
}

type Statuses = Readonly<Record<string, HistoryStatus>>;

const IDLE: HistoryStatus = {_tag: 'Idle'};
const earlierKey = (agentId: string): string => `earlier/${agentId}`;
const wholeKey = (agentId: string, ordinal: number): string => `whole/${agentId}/${ordinal}`;

export const makeAgentHistory = Effect.fn('AgentHistory.make')(function* (parts: HostParts) {
  const {client, store} = parts;
  const statuses = yield* SubscriptionRef.make<Statuses>({});
  const statusesAtom = Atom.keepAlive(Atom.subscriptionRef(statuses));

  const set = (key: string, status: HistoryStatus) =>
    SubscriptionRef.update(statuses, all => ({...all, [key]: status}));

  /** Marks `key` Loading and says whether this call is the one that should read. */
  const begin = (key: string) =>
    SubscriptionRef.modify(statuses, (all): [boolean, Statuses] =>
      all[key]?._tag === 'Loading' ? [false, all] : [true, {...all, [key]: {_tag: 'Loading'}}],
    );

  const fold = (frame: HostFrame) =>
    SubscriptionRef.update(store.ref, model => applyHostFrame(model, frame));

  /** Runs `read` as the status of `key`: Loading, then Idle or Failed. */
  const reading = <A>(key: string, read: Effect.Effect<A, AppError>) =>
    Effect.gen(function* () {
      if (!(yield* begin(key))) return;
      const done = yield* read.pipe(Effect.result);
      yield* set(
        key,
        done._tag === 'Success' ? IDLE : {_tag: 'Failed', fault: faultOf(done.failure)},
      );
    }).pipe(Effect.provideContext(parts.ambient));

  const loadEarlier = Effect.fn('AgentHistory.loadEarlier')((agentId: string, before: number) =>
    reading(
      earlierKey(agentId),
      client.listItems({agentId, beforeOrdinal: before, pageSize: AGENT_PAGE_SIZE}).pipe(
        Effect.flatMap(({page}) =>
          page === undefined
            ? Effect.void
            : fold({
                from: 'history',
                agentId,
                frame: {
                  sequence: page.sequence,
                  body: {case: 'older', value: {items: page.items, elisions: page.elisions}},
                },
              }),
        ),
      ),
    ),
  );

  const expand = Effect.fn('AgentHistory.expand')((agentId: string, ordinal: number) =>
    reading(
      wholeKey(agentId, ordinal),
      client.getItem({agentId, ordinal}).pipe(
        Effect.flatMap(({item, sequence}) =>
          item === undefined
            ? Effect.void
            : fold({
                from: 'history',
                agentId,
                frame: {sequence, body: {case: 'whole', value: {item}}},
              }),
        ),
      ),
    ),
  );

  const byKey = Atom.family((key: string) => Atom.make(get => get(statusesAtom)[key] ?? IDLE));
  const history: AgentHistory = {
    loadEarlier,
    expand,
    earlier: agentId => byKey(earlierKey(agentId)),
    whole: (agentId, ordinal) => byKey(wholeKey(agentId, ordinal)),
  };
  return history;
});
