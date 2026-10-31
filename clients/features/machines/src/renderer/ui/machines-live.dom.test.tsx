import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {SUBSCRIPTION_LINGER_MS} from '@vibesys/connection';
import {type FakeSubscribeStream, hubRig, settle} from '@vibesys/connection/testing';
import type {MachineDescription} from '@vibesys/hosts';
import type {MachineEntry} from '@vibesys/ipc';
import {backoffMs, failureBoundMs, machineStats, OFFLINE_AFTER} from '@vibesys/subscription';
import {machineEntry} from '@vibesys/subscription/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {initialMachines, type MachinesState, machinesStep} from '../../state/index.js';
import {createLiveStats} from '../effects/live.js';
import {MachinesPage} from './machines-page.js';

const window = new Window();
const installed = Object.getOwnPropertyNames(window).filter(name => !(name in globalThis));

beforeAll(() => {
  for (const name of installed) {
    Object.defineProperty(globalThis, name, {
      configurable: true,
      writable: true,
      value: Reflect.get(window, name),
    });
  }
  Object.assign(globalThis, {window, IS_REACT_ACT_ENVIRONMENT: true});
});

afterAll(async () => {
  for (const name of [...installed, 'window', 'IS_REACT_ACT_ENVIRONMENT']) {
    Reflect.deleteProperty(globalThis, name);
  }
  await window.happyDOM.close();
});

const A = 'ssh:host-a';
const B = 'ssh:host-b';
const BEAT = 5_000;
const AGE = machineStats.policy.staleAfterMs;

const FACTS: MachineDescription = {
  checkout: null,
  hardware: {cpu_cores: 16, gpus: [], accelerator: 'cpu'},
  docker: {reachable: true, version: '29.1'},
  harness_clis: [],
  runs: {live: 0, ended_today: 0},
};

/** The Machines store with `hosts` listed and each reported ready. */
function listed(hosts: readonly string[]): MachinesState {
  const entries: MachineEntry[] = hosts.map(host => ({host, label: host, checkout: '~/vibesys'}));
  let {state} = initialMachines();
  state = machinesStep(state, {type: 'listed', token: 1, result: {ok: true, value: entries}}).state;
  for (const machine of state.machines) {
    state = machinesStep(state, {
      type: 'reported',
      token: machine.checking ?? 0,
      host: machine.host,
      result: {ok: true, value: {kind: 'ready', facts: FACTS}},
    }).state;
  }
  return state;
}

/** The page over the real hub: fake time, a scripted gateway, and a window's source per card. */
function rig() {
  const {clock, gateway, subscriptions} = hubRig();
  const live = createLiveStats(subscriptions.port, clock.schedule, () => clock.now());
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const show = (shown: readonly string[]) =>
    act(async () =>
      root.render(
        <MachinesPage
          state={listed(shown)}
          dispatch={() => undefined}
          runCounts={new Map()}
          liveRuns={new Map()}
          live={live}
          showRuns={() => undefined}
          openSettings={() => undefined}
        />,
      ),
    );
  const card = (host: string): HTMLElement => {
    const heading = [...container.querySelectorAll('h2')].find(each => each.textContent === host);
    const found = heading?.closest('.vs-card');
    if (!(found instanceof window.HTMLElement)) throw new Error(`no card for ${host}`);
    return found as unknown as HTMLElement;
  };
  const streamOf = (host: string): FakeSubscribeStream => {
    const open = gateway.streams.filter(each => each.host === host && each.open).at(-1);
    if (open === undefined) throw new Error(`no stream open for ${host}`);
    return open;
  };
  /** Time passes for the hub and the page together; the page renders what that did. */
  const advance = (ms: number) =>
    act(async () => {
      clock.advanceBy(ms);
      await settle();
    });
  /** The host's gateway speaks; the page renders what it said. */
  const send = (host: string, speak: (stream: FakeSubscribeStream) => void) =>
    act(async () => {
      speak(streamOf(host));
      await settle();
    });
  const snapshot = (host: string, readings?: Parameters<typeof machineEntry>[2]) =>
    send(host, stream => {
      stream.reached('runs');
      stream.send({case: 'entry', value: machineEntry('machine', clock.now(), readings)});
      stream.send({case: 'synced', value: {}});
    });
  const press = (host: string, name: string) =>
    act(async () => {
      const button = [...card(host).querySelectorAll('button')].find(
        each => each.textContent === name,
      );
      if (button === undefined) throw new Error(`${host} has no ${name} button`);
      button.click();
      await settle();
    });
  return {
    clock,
    gateway,
    subscriptions,
    container,
    show,
    card,
    advance,
    send,
    snapshot,
    press,
    gauges: (host: string) => [...card(host).querySelectorAll('.vs-gauge')],
    dimmed: (host: string) =>
      [...card(host).querySelectorAll('.vs-gauge')].every(each => each.hasAttribute('data-dim')),
    text: (host: string) => card(host).textContent ?? '',
    stop: () => act(async () => root.unmount()),
  };
}

/** The host's attempts all fail before reaching it, until the page calls it failed. */
async function loseEveryStream(world: ReturnType<typeof rig>, host: string): Promise<void> {
  for (let loss = 1; loss <= OFFLINE_AFTER; loss += 1) {
    await world.send(host, stream => stream.fail());
    if (loss < OFFLINE_AFTER) await world.advance(backoffMs(loss));
  }
}

describe('the Machines page reads each host live', () => {
  test('a live host shows CPU and memory as they arrive, and a host that stops delivering is dimmed with its age at the bound', async () => {
    const world = rig();
    await world.show([A]);
    expect(world.text(A)).toContain('Connecting');
    await world.snapshot(A, {cpuPercent: 34, memory: {usedBytes: 2 ** 33, totalBytes: 2 ** 34}});
    expect(world.gauges(A).map(each => each.textContent)).toEqual([
      'CPU34%',
      'Memory8.0 / 16.0 GB',
    ]);
    expect(world.dimmed(A)).toBe(false);
    expect(world.text(A)).not.toContain('Stale');
    // A value arrives each beat and the page follows it.
    await world.advance(BEAT);
    await world.send(A, stream =>
      stream.send({
        case: 'entry',
        value: machineEntry('machine', world.clock.now(), {cpuPercent: 71}),
      }),
    );
    expect(world.gauges(A)[0]?.textContent).toBe('CPU71%');
    // Heartbeats prove the stream and nothing else: the value is dimmed exactly at the bound.
    for (let at = BEAT; at < AGE; at += BEAT) {
      await world.advance(BEAT);
      await world.send(A, stream => stream.send({case: 'heartbeat', value: {}}));
    }
    await world.advance(BEAT - 1);
    expect(world.dimmed(A)).toBe(false);
    await world.advance(1);
    expect(world.dimmed(A)).toBe(true);
    // Dimmed, never hidden: the value stays, with how old it is.
    expect(world.gauges(A)[0]?.textContent).toBe('CPU71%');
    expect(world.text(A)).toContain(`Stale ${Math.floor(AGE / 1000)} s`);
    await world.advance(12_000);
    expect(world.text(A)).toContain(`Stale ${Math.floor(AGE / 1000) + 12} s`);
    expect(world.dimmed(A)).toBe(true);
    // The host answers again: the value is current, and so it is shown.
    await world.send(A, stream => {
      stream.reached('runs');
      stream.send({
        case: 'entry',
        value: machineEntry('machine', world.clock.now(), {cpuPercent: 5}),
      });
      stream.send({case: 'synced', value: {}});
    });
    expect(world.gauges(A)[0]?.textContent).toBe('CPU5%');
    expect(world.dimmed(A)).toBe(false);
    expect(world.text(A)).not.toContain('Stale');
    await world.stop();
  });

  test('a host that never answers shows its state on its card, word by word, and never a banner', async () => {
    const world = rig();
    await world.show([A, B]);
    await world.snapshot(B);
    expect(world.text(A)).toContain('Connecting');
    // Its first attempt is lost at the open budget; the page says it is trying again.
    const bound = failureBoundMs(machineStats, 6 * 60_000);
    await world.advance(6 * 60_000);
    expect(world.text(A)).toContain('Retrying');
    await world.advance(bound);
    expect(world.text(A)).toContain('Unreachable');
    expect(world.text(A)).toContain('Retry');
    // The card is whole: still named, still able to open its settings. The other host is unaffected.
    expect(world.text(A)).toContain('Settings');
    expect(world.text(B)).toContain('CPU');
    expect(world.container.querySelector('[role="alert"], .vs-banner')).toBeNull();
    await world.stop();
  });

  test('Retry asks that host to try again at once, and no other', async () => {
    const world = rig();
    await world.show([A, B]);
    await loseEveryStream(world, A);
    await loseEveryStream(world, B);
    expect(world.text(A)).toContain('Unreachable');
    expect(world.text(B)).toContain('Unreachable');
    expect(world.gateway.open).toHaveLength(0);
    const before = world.gateway.streams.length;
    await world.press(A, 'Retry');
    expect(world.gateway.streams.slice(before).map(each => each.host)).toEqual([A]);
    expect(world.text(A)).toContain('Connecting');
    expect(world.text(B)).toContain('Unreachable');
    await world.stop();
  });

  test('removing a host closes its card’s subscription after the linger, and the hub closing it says Closed with no Retry', async () => {
    const world = rig();
    await world.show([A, B]);
    await world.snapshot(A);
    await world.snapshot(B);
    expect(world.gateway.open.map(each => each.host).sort()).toEqual([A, B]);
    await world.show([B]);
    // The card is gone; its stream outlives it by the linger, then goes. The other card keeps its own.
    await world.advance(SUBSCRIPTION_LINGER_MS - 1);
    expect(world.gateway.open.map(each => each.host).sort()).toEqual([A, B]);
    await world.advance(1);
    expect(world.gateway.open.map(each => each.host)).toEqual([B]);
    expect(world.gauges(B)).toHaveLength(2);
    // Main closing a host that is still on the page: its card says so, and offers no retry.
    await act(async () => {
      world.subscriptions.closeHost(B);
      await settle();
    });
    expect(world.text(B)).toContain('Closed');
    expect(world.text(B)).not.toContain('Retry');
    expect(world.gateway.open).toHaveLength(0);
    await world.stop();
  });

  test('a stale reason reads as the same dimmed value whichever way the stream was lost', async () => {
    for (const lose of [
      (stream: FakeSubscribeStream) => stream.fail(),
      (stream: FakeSubscribeStream) => stream.end(),
    ]) {
      const world = rig();
      await world.show([A]);
      await world.snapshot(A, {cpuPercent: 40});
      await world.send(A, lose);
      expect(world.dimmed(A)).toBe(true);
      expect(world.gauges(A)[0]?.textContent).toBe('CPU40%');
      await world.stop();
    }
  });
});
