/**
 * What an implementation of `SubscriptionPort` supplies to the port's contract suite: the port, and
 * the far side that plays the host. It holds no test runner, so every package's Fakes can build one.
 */
import type {HostKey} from '@vibesys/hosts';
import type {TopicEntry} from '@vibesys/proto';
import type {SubscriptionPort} from '../index.js';

/** The host as its owner sees it: what the port asks of it, and what it does to the port. */
export interface FarSide {
  /** Every observation of `host` is brought live with `entry` as its machine's stats. */
  live(host: HostKey, entry: TopicEntry): void;
  /** Whether the far side still serves any observation of `host`. */
  holds(host: HostKey): boolean;
  /** `host` has lost its stream and is waiting to try again. */
  waits(host: HostKey): Promise<void>;
  /** How many times `host` was asked to try again since it waited. */
  retried(host: HostKey): number;
  /** The host was removed: its observations end. */
  remove(host: HostKey): Promise<void>;
  /** Let outcomes in flight reach their owners. */
  drain(): Promise<void>;
  /** Let time pass for whatever the far side keeps a while after its last observer left. */
  later(): Promise<void>;
}

export interface PortRig {
  readonly port: SubscriptionPort;
  readonly far: FarSide;
}
