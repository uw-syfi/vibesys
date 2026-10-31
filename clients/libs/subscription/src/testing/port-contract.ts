/**
 * The contract every `SubscriptionPort` passes, whatever stands behind it: a Fake, the hub in
 * main, or the window's bridge over Electron's ports to that hub. An implementation supplies a
 * rig, the port and the far side that plays the host; the cases hold for all of them and skip
 * none. It runs under `bun:test`, so it has its own entry (`@vibesys/subscription/contract`) and
 * the testing barrel does not re-export it.
 */
import {describe, expect, test} from 'bun:test';
import type {HostKey} from '@vibesys/hosts';
import type {TopicEntry} from '@vibesys/proto';
import type {SubscriptionPort, SubscriptionUpdate} from '../index.js';
import {applyUpdate, INITIAL_WINDOW, machineStats, stateOf, type WindowState} from '../index.js';
import {machineEntry} from './entries.js';
import type {PortRig} from './port-rig.js';

export type {FarSide, PortRig} from './port-rig.js';

/** The stats of the one machine a host reports, sampled at `seconds`. */
const stats = (seconds: number): TopicEntry => machineEntry('machine', seconds * 1000);

const A: HostKey = 'ssh:host-a';
const B: HostKey = 'ssh:host-b';

/** An observation as a window holds it: the updates it was sent, folded as a window folds them. */
class Observation {
  readonly updates: SubscriptionUpdate[] = [];
  readonly ends: (string | undefined)[] = [];
  window: WindowState = INITIAL_WINDOW;
  readonly call: {cancel(): void};

  constructor(port: SubscriptionPort, host: HostKey, key?: string) {
    this.call = port.observe(
      host,
      machineStats.request,
      key,
      update => {
        this.updates.push(update);
        this.window = applyUpdate(machineStats, this.window, update);
      },
      error => this.ends.push(error),
    );
  }

  get state() {
    return stateOf(machineStats, this.window);
  }
}

/** The cases: what any port does for a window, each over a fresh rig. */
const CASES: Readonly<Record<string, (rig: PortRig) => Promise<void>>> = {
  'an observation folds to what the far side sent: live, with its value': async ({port, far}) => {
    const seen = new Observation(port, A);
    await far.drain();
    expect(seen.state.kind).not.toBe('live');
    far.live(A, stats(1));
    await far.drain();
    expect(seen.state).toMatchObject({kind: 'live'});
    if (seen.state.kind !== 'live') return;
    expect(seen.state.value.sampledAt?.seconds).toBe(1n);
    expect(seen.ends).toEqual([]);
  },

  'cancelling stops what is delivered and lets the far side go, and ends nothing': async ({
    port,
    far,
  }) => {
    const seen = new Observation(port, A);
    far.live(A, stats(1));
    await far.drain();
    const before = seen.updates.length;
    seen.call.cancel();
    seen.call.cancel();
    await far.later();
    expect(far.holds(A)).toBe(false);
    far.live(A, stats(2));
    await far.drain();
    expect(seen.updates).toHaveLength(before);
    expect(seen.ends).toEqual([]);
  },

  'observers of one host share it: it is held until the last one has gone': async ({port, far}) => {
    const first = new Observation(port, A);
    const second = new Observation(port, A);
    first.call.cancel();
    await far.later();
    expect(far.holds(A)).toBe(true);
    far.live(A, stats(3));
    await far.drain();
    expect(second.state.kind).toBe('live');
    expect(first.state.kind).not.toBe('live');
    second.call.cancel();
    await far.later();
    expect(far.holds(A)).toBe(false);
  },

  'a removed host ends its observations once, with nothing after, and leaves other hosts be':
    async ({port, far}) => {
      const gone = new Observation(port, A);
      const kept = new Observation(port, B);
      far.live(A, stats(1));
      far.live(B, stats(1));
      await far.drain();
      await far.remove(A);
      expect(gone.ends).toHaveLength(1);
      expect(gone.state).toEqual({kind: 'failed', reason: 'closed', retrying: false});
      const heard = gone.updates.length;
      far.live(A, stats(2));
      await far.drain();
      gone.call.cancel();
      expect(gone.updates).toHaveLength(heard);
      expect(gone.ends).toHaveLength(1);
      expect(kept.ends).toEqual([]);
      expect(kept.state.kind).toBe('live');
      expect(far.holds(B)).toBe(true);
    },

  'a retry reaches the asked host and no other, and one for a host nobody watches is harmless':
    async ({port, far}) => {
      new Observation(port, A);
      new Observation(port, B);
      await far.drain();
      await far.waits(A);
      await far.waits(B);
      port.retry(A);
      await far.drain();
      expect(far.retried(A)).toBe(1);
      expect(far.retried(B)).toBe(0);
      port.retry('ssh:host-c');
      port.retry('local');
      await far.drain();
      expect(far.retried(A)).toBe(1);
      expect(far.retried(B)).toBe(0);
      expect(far.holds('ssh:host-c')).toBe(false);
    },

  'a key limits what an observation is sent, and a retry does not end it': async ({port, far}) => {
    const keyed = new Observation(port, A, 'machine');
    far.live(A, stats(1));
    await far.drain();
    expect(keyed.state.kind).toBe('live');
    await far.waits(A);
    port.retry(A);
    await far.drain();
    expect(keyed.ends).toEqual([]);
  },
};

export function subscriptionPortContract(name: string, make: () => PortRig): void {
  describe(`SubscriptionPort: ${name}`, () => {
    for (const [title, run] of Object.entries(CASES)) test(title, () => run(make()));
  });
}
