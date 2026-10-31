/**
 * Main's hub of live subscriptions: one machine per host and topic instance, shared by every
 * window and feature that watches it. The hub owns the clock and the retries; what it hands each
 * observer is the machine's data updates and its control state, which the observer folds with the
 * same reducer (`@vibesys/subscription`). The first observer starts the stream and the last one's
 * leaving stops it, so nothing runs for nobody.
 */
import {fromBinary, toBinary} from '@bufbuild/protobuf';
import type {HostKey, ListingStep} from '@vibesys/hosts';
import {clockFrom} from '@vibesys/machine';
import {
  type SubscribeRequest,
  SubscribeRequestSchema,
  SubscribeResponseSchema,
} from '@vibesys/proto';
import {
  type AnyTopic,
  type ConnectStep,
  type Control,
  controlOf,
  type FailureReason,
  type Instant,
  replayUpdates,
  type StreamOperation,
  SUBSCRIBE_METHOD,
  type SubscriptionPort,
  type SubscriptionSnapshot,
  type SubscriptionState,
  type SubscriptionUpdate,
  streamEvent,
  subscriptionMachine,
  subscriptionSource,
  type Topic,
  topicOf,
} from '@vibesys/subscription';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {type Actor, createActor, fromPromise} from 'xstate';
import {GATEWAY_FIRST_CONNECT_BUDGET_MS} from '../state/gateway.js';
import type {GatewayCalls} from './gateway-calls.js';

/** What the hub needs of the gateway: open a stream, report its steps, end when it ends. */
export type SubscriptionStreams = Pick<GatewayCalls, 'stream'>;

/** The gateway's steps toward a call, as a subscription words them. */
const CONNECT_STEPS: Readonly<Record<ListingStep, ConnectStep>> = {
  gateway: 'gateway',
  handshake: 'handshake',
  runs: 'stream',
};

/**
 * How long a stream outlives its last observer. A page that is left and opened again (a screen
 * switch, a remount) finds its stream running, instead of dialing the host again, which for an
 * ssh host is a new connection. Nothing needs the stream after this, so it stops.
 */
export const SUBSCRIPTION_LINGER_MS = 5_000;

/** A subscription that gave a screen no value, and why. */
export class SubscriptionFailed extends Error {
  constructor(
    readonly reason: FailureReason,
    readonly retrying: boolean,
  ) {
    super(`subscription failed: ${reason}`);
  }
}

interface Observer {
  readonly key: string | undefined;
  readonly receive: (update: SubscriptionUpdate) => void;
  readonly ended: (error?: string) => void;
}

interface InstanceParts {
  readonly host: HostKey;
  /** What the machine folds with: the registry's descriptor of the request's topic. */
  readonly topic: AnyTopic;
  /** What the machine opens, and what the instance is keyed by: the one request, validated. */
  readonly request: SubscribeRequest;
  readonly streams: SubscriptionStreams;
  readonly schedule: ScheduleTimeout;
  readonly now: () => Instant;
  readonly retired: (instance: Instance) => void;
  readonly report: (error: unknown) => void;
}

/** One machine and the observers it serves. */
class Instance {
  readonly observers = new Set<Observer>();
  readonly #actor: Actor<typeof subscriptionMachine>;
  #control: Control;
  /** Cancels the pending stop of a machine that has no observer, or null while it has one. */
  #lingering: (() => void) | null = null;

  readonly #parts: InstanceParts;

  constructor(parts: InstanceParts) {
    this.#parts = parts;
    const {host, topic, request, streams, schedule, now} = parts;
    const bytes = toBinary(SubscribeRequestSchema, request);
    this.#actor = createActor(
      subscriptionMachine.provide({
        actors: {
          stream: fromPromise<void, StreamOperation>(({input, signal}) =>
            streams.stream(
              host,
              SUBSCRIBE_METHOD,
              bytes,
              signal,
              frame => {
                // A stream that was left may still speak; it no longer speaks for the machine.
                if (!signal.aborted)
                  input.receive(streamEvent(fromBinary(SubscribeResponseSchema, frame), now()));
              },
              step => {
                if (!signal.aborted) input.receive({type: 'reached', step: CONNECT_STEPS[step]});
              },
            ),
          ),
        },
      }),
      {input: {topic, openBudgetMs: GATEWAY_FIRST_CONNECT_BUDGET_MS}, clock: clockFrom(schedule)},
    );
    this.#control = controlOf(this.#actor.getSnapshot());
    // The machine emits its data before the snapshot that carries its control, and that order is
    // what every observer folds.
    this.#actor.on('update', event => this.#deliver(event.update));
    this.#actor.subscribe(snapshot => this.#changed(snapshot));
    this.#actor.start();
  }

  /**
   * Add an observer and bring it up to date: the data it missed, then the control that goes with
   * it. It is a member before the first delivery, so it is released like any other whatever that
   * delivery does, and the replay is delivered as every later update is.
   */
  join(observer: Observer): void {
    this.#lingering?.();
    this.#lingering = null;
    this.observers.add(observer);
    const {fold} = this.#actor.getSnapshot().context;
    for (const update of replayUpdates(fold, observer.key)) this.#send(observer, update);
    this.#send(observer, {kind: 'control', control: this.#control});
  }

  leave(observer: Observer): void {
    this.observers.delete(observer);
    if (this.observers.size > 0 || this.#lingering !== null) return;
    this.#lingering = this.#parts.schedule(() => {
      this.#lingering = null;
      this.close();
    }, SUBSCRIPTION_LINGER_MS);
  }

  send(event: {type: 'retry'} | {type: 'wake'} | {type: 'close'}): void {
    this.#actor.send(event);
  }

  /** Stop the machine, which aborts its stream. */
  close(): void {
    this.#lingering?.();
    this.#lingering = null;
    this.#actor.stop();
    this.#parts.retired(this);
  }

  #changed(snapshot: SubscriptionSnapshot): void {
    const control = controlOf(snapshot);
    if (sameControl(control, this.#control)) {
      if (snapshot.status === 'done') this.#finish();
      return;
    }
    this.#control = control;
    this.#deliver({kind: 'control', control});
    if (snapshot.status === 'done') this.#finish();
  }

  /** The machine is final: it was closed from this side. */
  #finish(): void {
    this.#lingering?.();
    this.#lingering = null;
    const observers = [...this.observers];
    this.observers.clear();
    this.#parts.retired(this);
    for (const observer of observers) observer.ended();
  }

  /** An observer cancelled by an earlier observer's callback in the same update hears nothing. */
  #deliver(update: SubscriptionUpdate): void {
    for (const observer of [...this.observers]) {
      if (this.observers.has(observer)) this.#send(observer, update);
    }
  }

  /**
   * Observers are isolated from the machine and from each other: one that throws is reported and
   * the rest still hear the update, because an exception inside the actor would end it.
   */
  #send(observer: Observer, update: SubscriptionUpdate): void {
    if (!wants(observer, update)) return;
    try {
      observer.receive(update);
    } catch (cause) {
      this.#parts.report(cause);
    }
  }
}

/** An observer of one key hears only that key's entries and removals. */
function wants(observer: Observer, update: SubscriptionUpdate): boolean {
  if (observer.key === undefined) return true;
  if (update.kind === 'entry') return update.entry.key === observer.key;
  if (update.kind === 'removed') return update.key === observer.key;
  return true;
}

function sameControl(left: Control, right: Control): boolean {
  return JSON.stringify(left) === JSON.stringify(right);
}

export class Subscriptions {
  readonly #instances = new Map<string, Instance>();
  #closed = false;

  constructor(
    private readonly streams: SubscriptionStreams,
    private readonly schedule: ScheduleTimeout,
    private readonly now: () => Instant,
    /** Told what an observer threw; the hub itself carries on. */
    private readonly report: (error: unknown) => void = () => {},
  ) {}

  /**
   * Relay `request` on `host`: the machine's data and control are sent to `receive` now and as
   * they change, restricted to `key` when given, for an observer that folds them itself (a window).
   * `ended` is told once, when the hub closes the host or itself. Cancelling releases the observer;
   * the last release stops the stream.
   */
  relay: SubscriptionPort['observe'] = (host, request, key, receive, ended) => {
    const topic = topicOf(request);
    if (this.#closed || topic === null) {
      ended(this.#closed ? 'subscriptions closed' : 'unknown subscription topic');
      return {cancel: () => {}};
    }
    const name = `${host}\0${Buffer.from(toBinary(SubscribeRequestSchema, request)).toString('base64')}`;
    return this.#join(name, host, topic, request, {key, receive, ended});
  };

  #join(
    name: string,
    host: HostKey,
    topic: AnyTopic,
    request: SubscribeRequest,
    observer: Observer,
  ): {cancel(): void} {
    let instance = this.#instances.get(name);
    if (instance === undefined) {
      instance = new Instance({
        host,
        topic,
        request,
        streams: this.streams,
        schedule: this.schedule,
        now: this.now,
        report: this.report,
        retired: retired => {
          if (this.#instances.get(name) === retired) this.#instances.delete(name);
        },
      });
      this.#instances.set(name, instance);
    }
    const joined = instance;
    let released = false;
    // Joining cannot throw: the replay is delivered as every later update is, with an observer's
    // failure reported and the observer kept, so the handle below always reaches its owner.
    joined.join(observer);
    return {
      cancel: () => {
        if (released) return;
        released = true;
        joined.leave(observer);
      },
    };
  }

  /** The relay as a window's port, for what serves windows. */
  get port(): SubscriptionPort {
    return {observe: this.relay, retry: host => this.retry(host)};
  }

  /** `listener` hears the state now and on each change; the returned function releases it. */
  observe<S>(
    host: HostKey,
    topic: Topic<S>,
    listener: (state: SubscriptionState<S>) => void,
    key?: string,
  ): () => void {
    const source = subscriptionSource(this.port, host, topic, key);
    const release = source.subscribe(() => listener(source.getSnapshot()));
    try {
      listener(source.getSnapshot());
    } catch (error) {
      // The caller gets no handle from a call that throws, so nothing else could release it.
      release();
      throw error;
    }
    return release;
  }

  /** The topic's value once the host says it is live, or the reason it will not. */
  first<S>(host: HostKey, topic: Topic<S>, signal: AbortSignal): Promise<S> {
    return new Promise<S>((resolve, reject) => {
      let release: (() => void) | undefined;
      let settled = false;
      const settle = (outcome: () => void): void => {
        if (settled) return;
        settled = true;
        registration.detach();
        // The watch is released after the delivery that settled it, never inside it.
        queueMicrotask(() => release?.());
        outcome();
      };
      const registration = registerAbort(signal, () =>
        settle(() => reject(signal.reason ?? new Error('cancelled'))),
      );
      if (registration.aborted) {
        settle(() => reject(signal.reason ?? new Error('cancelled')));
        return;
      }
      release = this.observe(host, topic, state => {
        if (state.kind === 'live') settle(() => resolve(state.value));
        else if (state.kind === 'failed')
          settle(() => reject(new SubscriptionFailed(state.reason, state.retrying)));
      });
    });
  }

  /** Try again now where a host is waiting or has stopped; a delivering stream is left be. */
  retry(host: HostKey): void {
    for (const instance of this.#of(host)) instance.send({type: 'retry'});
  }

  /** The machine woke: check each live stream within a beat, and retry the waiting ones at once. */
  wake(): void {
    for (const instance of this.#instances.values()) instance.send({type: 'wake'});
  }

  /** The host was removed: its observers hear `failed closed` and their streams stop. */
  closeHost(host: HostKey): void {
    for (const instance of this.#of(host)) instance.send({type: 'close'});
  }

  close(): void {
    this.#closed = true;
    for (const instance of [...this.#instances.values()]) instance.send({type: 'close'});
  }

  #of(host: HostKey): Instance[] {
    return [...this.#instances].flatMap(([name, instance]) =>
      name.startsWith(`${host}\0`) ? [instance] : [],
    );
  }
}
