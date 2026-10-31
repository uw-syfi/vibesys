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
} from '@vibesys/subscription';
import {machineEntry, type Readings} from './machine-entry.js';

/** What a scripted host is doing, as a window should see it. */
export type HostFeed =
  | {readonly kind: 'live'; readonly readings?: Readings}
  /** The last value is `ageMs` old at the time the window opens. */
  | {
      readonly kind: 'stale';
      readonly readings?: Readings;
      readonly reason: StaleReason;
      readonly ageMs: number;
    }
  | {readonly kind: 'failed'; readonly reason: FailureReason; readonly retrying: boolean}
  | {readonly kind: 'connecting'; readonly step: ConnectStep; readonly attempt: number};

export interface SubscriptionSeed {
  readonly hosts: Readonly<Record<string, HostFeed>>;
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
          control: {kind: 'stale', reason: feed.reason, attempt: 1, valueAt: now - feed.ageMs},
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

interface Observer {
  readonly host: string;
  readonly receive: (bytes: Uint8Array) => void;
}

class FixtureSubscriptions {
  readonly observers = new Set<Observer>();
  readonly retried: string[] = [];
  readonly #feeds = new Map<string, HostFeed>();

  constructor(private readonly seed: SubscriptionSeed) {
    for (const [host, feed] of Object.entries(seed.hosts)) this.#feeds.set(host, feed);
  }

  /** Send `feed` to every window observing `host`, as main sends a change. */
  set(host: string, feed: HostFeed): void {
    this.#feeds.set(host, feed);
    for (const observer of this.observers) {
      if (observer.host === host) this.#send(observer, feed);
    }
  }

  #send(observer: Observer, feed: HostFeed): void {
    for (const update of updatesOf(feed, Date.now()))
      observer.receive(gatewayReply(encodeUpdate(update)));
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
        const observer = {host: call.host, receive};
        this.observers.add(observer);
        const feed = this.#feeds.get(call.host);
        if (feed === undefined) receive(gatewayEnd(`no scripted host: ${call.host}`));
        else this.#send(observer, feed);
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
      retried: () => [...fixture.retried],
      observing: (host: string) =>
        [...fixture.observers].filter(observer => observer.host === host).length,
    },
  });
}
