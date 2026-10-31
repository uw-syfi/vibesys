/**
 * Browser fixture for the subscription port the Electron preload exposes (`vibesysSubscription`):
 * a page-side main that answers each observation with the updates a scripted host would send, and
 * records the retries a window asked for. It speaks the real wire (the generated messages through
 * `@vibesys/ipc`), so the window's source, fold and screens run unchanged.
 */
import {
  encodeUpdate,
  type GatewayPortBridge,
  gatewayEnd,
  gatewayReply,
  parseSubscriptionCall,
} from '@vibesys/ipc';
import type {
  ConnectStep,
  FailureReason,
  StaleReason,
  SubscriptionUpdate,
  TopicArm,
} from '@vibesys/subscription';
import {machineEntry, type Readings, runEntry} from './machine-entry.js';

/** What a scripted host is doing, as a window should see it. */
export type HostFeed =
  | {readonly kind: 'live'; readonly readings?: Readings}
  /**
   * The last value is `ageMs` old at the time the window opens. `attempt` 0 is a stream being opened
   * right now (after a Retry), and from 1 one that was lost and is waited for.
   */
  | {
      readonly kind: 'stale';
      readonly readings?: Readings;
      readonly reason: StaleReason;
      readonly ageMs: number;
      readonly attempt?: number;
    }
  | {readonly kind: 'failed'; readonly reason: FailureReason; readonly retrying: boolean}
  | {readonly kind: 'connecting'; readonly step: ConnectStep; readonly attempt: number};

/**
 * What a scripted host says of its runs: how many have a writer and how many ended. Stale means
 * the host stopped saying it `ageMs` ago, and the window holds these counts until it speaks again.
 */
export interface RunsFeed {
  readonly alive: number;
  readonly ended: number;
  readonly stale?: {
    readonly reason: StaleReason;
    readonly ageMs: number;
    readonly attempt?: number;
  };
}

export interface SubscriptionSeed {
  /** What each host's machine stats do. */
  readonly hosts: Readonly<Record<string, HostFeed>>;
  /** What each host says of its runs: the one source of a card's run counts. */
  readonly runs?: Readonly<Record<string, RunsFeed>>;
  /** What a host does once a window has asked it to retry. */
  readonly afterRetry?: Readonly<Record<string, HostFeed>>;
}

const SAMPLED_MS = 1_760_000_000_000;

function updatesOf(feed: HostFeed, now: number): readonly SubscriptionUpdate[] {
  const sample = (readings: Readings | undefined): SubscriptionUpdate[] => [
    {kind: 'begin'},
    {kind: 'entry', entry: machineEntry('machine', SAMPLED_MS, readings)},
    {kind: 'synced'},
  ];
  switch (feed.kind) {
    case 'live':
      return [...sample(feed.readings), {kind: 'control', control: {kind: 'live', since: now}}];
    case 'stale':
      return [
        ...sample(feed.readings),
        {
          kind: 'control',
          control: {
            kind: 'stale',
            reason: feed.reason,
            attempt: feed.attempt ?? 1,
            valueAt: now - feed.ageMs,
          },
        },
      ];
    case 'failed':
      return [
        {kind: 'control', control: {kind: 'failed', reason: feed.reason, retrying: feed.retrying}},
      ];
    case 'connecting':
      return [
        {kind: 'begin'},
        {kind: 'control', control: {kind: 'connecting', step: feed.step, attempt: feed.attempt}},
      ];
  }
}

function runUpdates(feed: RunsFeed, now: number): readonly SubscriptionUpdate[] {
  const entries = Array.from({length: feed.alive + feed.ended}, (_, index) => ({
    kind: 'entry' as const,
    entry: runEntry(`run-${index}`, index < feed.alive ? 'alive' : 'ended'),
  }));
  const control: SubscriptionUpdate =
    feed.stale === undefined
      ? {kind: 'control', control: {kind: 'live', since: now}}
      : {
          kind: 'control',
          control: {
            kind: 'stale',
            reason: feed.stale.reason,
            attempt: feed.stale.attempt ?? 1,
            valueAt: now - feed.stale.ageMs,
          },
        };
  return [{kind: 'begin'}, ...entries, {kind: 'synced'}, control];
}

interface Observer {
  readonly host: string;
  readonly arm: TopicArm;
  readonly receive: (bytes: Uint8Array) => void;
}

class FixtureSubscriptions {
  readonly observers = new Set<Observer>();
  readonly retried: string[] = [];
  readonly #feeds = new Map<string, HostFeed>();
  readonly #runs = new Map<string, RunsFeed>();

  constructor(private readonly seed: SubscriptionSeed) {
    for (const [host, feed] of Object.entries(seed.hosts)) this.#feeds.set(host, feed);
    for (const [host, feed] of Object.entries(seed.runs ?? {})) this.#runs.set(host, feed);
  }

  /** Send `feed` to every window observing `host`'s machine stats, as main sends a change. */
  set(host: string, feed: HostFeed): void {
    this.#feeds.set(host, feed);
    for (const observer of this.observers) {
      if (observer.host === host && observer.arm === 'machineStats')
        this.#send(observer, updatesOf(feed, Date.now()));
    }
  }

  /** Send `feed` to every window observing `host`'s runs. */
  setRuns(host: string, feed: RunsFeed): void {
    this.#runs.set(host, feed);
    for (const observer of this.observers) {
      if (observer.host === host && observer.arm === 'runs')
        this.#send(observer, runUpdates(feed, Date.now()));
    }
  }

  /** What a window opening `arm` of `host` is told first; undefined when the world scripts none. */
  #updatesFor(arm: TopicArm, host: string, now: number): readonly SubscriptionUpdate[] | undefined {
    if (arm === 'runs') {
      const feed = this.#runs.get(host);
      return feed === undefined ? undefined : runUpdates(feed, now);
    }
    const feed = this.#feeds.get(host);
    return feed === undefined ? undefined : updatesOf(feed, now);
  }

  #send(observer: Observer, updates: readonly SubscriptionUpdate[]): void {
    for (const update of updates) observer.receive(gatewayReply(encodeUpdate(update)));
  }

  port(): GatewayPortBridge {
    return {
      open: (encoded, receive) => {
        const call = parseSubscriptionCall(encoded);
        if (call.kind === 'retry') {
          this.retried.push(call.host);
          const next = this.seed.afterRetry?.[call.host];
          receive(gatewayEnd());
          if (next !== undefined) this.set(call.host, next);
          return {cancel: () => undefined};
        }
        const arm = call.request.topic.case;
        if (arm !== 'machineStats' && arm !== 'runs')
          throw new Error(`the fixture host does not serve ${String(arm)}`);
        const observer = {host: call.host, arm, receive};
        this.observers.add(observer);
        const updates = this.#updatesFor(arm, call.host, Date.now());
        if (updates === undefined) receive(gatewayEnd(`no scripted ${arm}: ${call.host}`));
        else this.#send(observer, updates);
        return {cancel: () => void this.observers.delete(observer)};
      },
    };
  }
}

export function installSubscriptions(seed: SubscriptionSeed): void {
  const fixture = new FixtureSubscriptions(seed);
  Object.assign(window, {
    vibesysSubscription: fixture.port(),
    fakeSubscriptions: {
      set: (host: string, feed: HostFeed) => fixture.set(host, feed),
      setRuns: (host: string, feed: RunsFeed) => fixture.setRuns(host, feed),
      retried: () => [...fixture.retried],
      observing: (host: string, arm: TopicArm = 'machineStats') =>
        [...fixture.observers].filter(observer => observer.host === host && observer.arm === arm)
          .length,
    },
  });
}
