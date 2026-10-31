import {afterAll, beforeAll, describe, expect, test} from 'bun:test';
import {SUBSCRIPTION_LINGER_MS} from '@vibesys/connection';
import {type FakeSubscribeStream, hubRig, settle} from '@vibesys/connection/testing';
import type {MachineDescription} from '@vibesys/hosts';
import type {MachineEntry, MachineReport} from '@vibesys/ipc';
import {
  backoffMs,
  CONNECTING,
  failureBoundMs,
  type HostRunFacts,
  machineStats,
  OFFLINE_AFTER,
  runs as runsTopic,
  type SubscriptionSource,
  type TopicArm,
} from '@vibesys/subscription';
import {machineEntry, runEntryOf} from '@vibesys/subscription/testing';
import {Window} from 'happy-dom';
import {act} from 'react';
import {createRoot} from 'react-dom/client';
import {initialMachines, type MachinesState, machinesStep} from '../../state/index.js';
import {createLiveStats} from '../effects/live.js';
import {useDeliveringHosts, WithHostLive} from '../host-stats.js';
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
function listed(
  hosts: readonly string[],
  report: MachineReport = {kind: 'ready', facts: FACTS},
): MachinesState {
  const entries: MachineEntry[] = hosts.map(host => ({host, label: host, checkout: '~/vibesys'}));
  let {state} = initialMachines();
  state = machinesStep(state, {type: 'listed', token: 1, result: {ok: true, value: entries}}).state;
  for (const machine of state.machines) {
    state = machinesStep(state, {
      type: 'reported',
      token: machine.checking ?? 0,
      host: machine.host,
      result: {ok: true, value: report},
    }).state;
  }
  return state;
}

/** A runs source that never opens a stream: for the tests whose subject is the stats stream. */
const NO_RUNS: SubscriptionSource<HostRunFacts> = {
  subscribe: () => () => {},
  getSnapshot: () => CONNECTING,
  retry: () => {},
};

/**
 * The page over the real hub: fake time, a scripted gateway, and a window's source per card. Its
 * run counts come from the `runs` topic when `withRuns`, and otherwise nothing opens that stream.
 */
function rig(withRuns = false) {
  const {clock, gateway, subscriptions} = hubRig();
  const real = createLiveStats(subscriptions.port, clock.schedule, () => clock.now());
  const live = withRuns ? real : {...real, runs: () => NO_RUNS};
  const container = document.createElement('div');
  document.body.append(container);
  const root = createRoot(container);
  const show = (shown: readonly string[], report?: MachineReport) =>
    act(async () =>
      root.render(
        <MachinesPage
          state={listed(shown, report)}
          dispatch={() => undefined}
          live={(host, render) => <WithHostLive live={live} host={host} render={render} />}
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
  const streamOf = (host: string, arm: TopicArm = 'machineStats'): FakeSubscribeStream => {
    const open = gateway.streams
      .filter(each => each.host === host && each.open && each.request.topic.case === arm)
      .at(-1);
    if (open === undefined) throw new Error(`no ${arm} stream open for ${host}`);
    return open;
  };
  /** Time passes for the hub and the page together; the page renders what that did. */
  const advance = (ms: number) =>
    act(async () => {
      clock.advanceBy(ms);
      await settle();
    });
  /** The host's gateway speaks; the page renders what it said. */
  const send = (host: string, speak: (stream: FakeSubscribeStream) => void, arm?: TopicArm) =>
    act(async () => {
      speak(streamOf(host, arm));
      await settle();
    });
  /** The host says which of its runs are alive and which ended: a whole snapshot. */
  const runs = (host: string, alive: number, ended: number) =>
    send(
      host,
      stream => {
        for (let each = 0; each < alive + ended; each += 1)
          stream.send({
            case: 'entry',
            value: runEntryOf(`run-${each}`, each < alive ? 'alive' : 'ended'),
          });
        stream.send({case: 'synced', value: {}});
      },
      'runs',
    );
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
  /** The rail's reading: which hosts deliver, as `useDeliveringHosts` says it, in its own root. */
  const rail = (shown: readonly string[], watching: boolean) => {
    const seen: ReadonlySet<string>[] = [];
    const reports: (readonly string[])[] = [];
    const report = (hosts: readonly string[]): void => void reports.push([...hosts].sort());
    const {machines} = listed(shown);
    const Reading = (): null => {
      seen.push(useDeliveringHosts(live, machines, watching, report));
      return null;
    };
    const place = document.createElement('div');
    document.body.append(place);
    const hosts = createRoot(place);
    return {
      mount: () => act(async () => hosts.render(<Reading />)),
      latest: () => [...(seen.at(-1) ?? [])].sort(),
      reported: () => reports,
      unmount: () => act(async () => hosts.unmount()),
    };
  };
  return {
    clock,
    gateway,
    subscriptions,
    rail,
    container,
    show,
    card,
    advance,
    send,
    snapshot,
    runs,
    press,
    gauges: (host: string) => [...card(host).querySelectorAll('.vs-gauge')],
    /** The card's one status: exactly one chip, whatever the state. */
    status: (host: string) => {
      const chips = [...card(host).querySelectorAll('.vs-chip')];
      if (chips.length !== 1) throw new Error(`${host} shows ${chips.length} statuses`);
      return chips[0]?.textContent ?? '';
    },
    retryButton: (host: string) =>
      [...card(host).querySelectorAll('button')].find(each => each.textContent === 'Retry'),
    ageText: (host: string) =>
      /(\d+ (?:s|min|h)) ago/.exec(card(host).textContent ?? '')?.[1] ?? null,
    dimmed: (host: string) =>
      [...card(host).querySelectorAll('.vs-gauge')].every(each => each.hasAttribute('data-dim')),
    text: (host: string) => card(host).textContent ?? '',
    pressable: (host: string, name: string) =>
      [...card(host).querySelectorAll('button')].some(each => each.textContent === name),
    /** The hardware and run cells of the card's summary line, in order, and whether its meter is dimmed. */
    summary: (host: string) => {
      const meter = card(host).querySelector('.vs-meter[role="img"]');
      const row = meter?.parentElement?.parentElement;
      if (meter === null || meter === undefined || row === null || row === undefined)
        throw new Error(`${host} has no live meter in its summary`);
      return {
        cells: [...row.children].map(each => each.textContent ?? ''),
        dim: meter.hasAttribute('data-dim'),
      };
    },
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

/** The first values of a host, and then values that arrive each beat. */
async function liveWith(world: ReturnType<typeof rig>, host: string, cpuPercent: number) {
  await world.snapshot(host, {cpuPercent, memory: {usedBytes: 2 ** 33, totalBytes: 2 ** 34}});
}

describe('the Machines page reads each host live', () => {
  test('a live host shows CPU and memory as they arrive, and a host that stops delivering is dimmed with its age at the bound', async () => {
    const world = rig();
    await world.show([A]);
    expect(world.status(A)).toBe('Connecting');
    await liveWith(world, A, 34);
    expect(world.gauges(A).map(each => each.textContent)).toEqual([
      'CPU34%',
      'Memory8.0 / 16.0 GB',
    ]);
    expect(world.status(A)).toBe('Online');
    expect(world.dimmed(A)).toBe(false);
    expect(world.ageText(A)).toBeNull();
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
    // Dimmed, never hidden: the value stays, the status says so, and the age counts up.
    expect(world.gauges(A)[0]?.textContent).toBe('CPU71%');
    expect(world.status(A)).toBe('Stale');
    expect(world.ageText(A)).toBe(`${Math.floor(AGE / 1000)} s`);
    await world.advance(12_000);
    expect(world.ageText(A)).toBe(`${Math.floor(AGE / 1000) + 12} s`);
    // A listener hears the same: a meter, its value, and that it is stale.
    expect(world.gauges(A)[0]?.getAttribute('role')).toBe('meter');
    expect(world.gauges(A)[0]?.getAttribute('aria-valuenow')).toBe('71');
    expect(world.gauges(A)[0]?.getAttribute('aria-valuetext')).toBe(
      `71%, stale ${Math.floor(AGE / 1000) + 12} s`,
    );
    // The host answers again: the value is current, and so it is shown.
    await world.snapshot(A, {cpuPercent: 5});
    expect(world.gauges(A)[0]?.textContent).toBe('CPU5%');
    expect(world.dimmed(A)).toBe(false);
    expect(world.status(A)).toBe('Online');
    expect(world.ageText(A)).toBeNull();
    await world.stop();
  });

  test('a section the host could not read is `–`, announced as unknown, never zero', async () => {
    const world = rig();
    await world.show([A]);
    await world.snapshot(A, {cpuPercent: null, memory: null, gpus: null});
    expect(world.gauges(A).map(each => each.textContent)).toEqual([
      'CPU–',
      'Memory–',
      'GPU–',
      'VRAM–',
    ]);
    // Not a meter with a value, which would say zero: an unknown, by name.
    expect(world.gauges(A).map(gauge => gauge.getAttribute('aria-label'))).toEqual([
      'CPU unknown',
      'Memory unknown',
      'GPU unknown',
      'VRAM unknown',
    ]);
    for (const gauge of world.gauges(A)) {
      expect(gauge.getAttribute('role')).toBe('img');
      expect(gauge.hasAttribute('aria-valuenow')).toBe(false);
    }
    await world.stop();
  });

  test('a host that never answers shows its state in its one status, word by word, and never a banner', async () => {
    const world = rig();
    await world.show([A]);
    expect(world.status(A)).toBe('Connecting');
    // Its first attempt is lost at the open budget; the page says it is still trying.
    const bound = failureBoundMs(machineStats, 6 * 60_000);
    await world.advance(6 * 60_000);
    expect(world.status(A)).toBe('Connecting');
    expect(world.retryButton(A)).toBeUndefined();
    await world.advance(bound);
    expect(world.status(A)).toBe('Unreachable');
    expect(world.retryButton(A)).toBeDefined();
    // The card is whole: still named, still able to open its settings.
    expect(world.text(A)).toContain('Settings');
    expect(world.container.querySelector('[role="alert"], .vs-banner')).toBeNull();
    await world.stop();
  });

  test('Retry asks that host to try again at once and no other, and each is named for its machine', async () => {
    const world = rig();
    await world.show([A, B]);
    await loseEveryStream(world, A);
    await loseEveryStream(world, B);
    expect(world.status(A)).toBe('Unreachable');
    expect(world.status(B)).toBe('Unreachable');
    expect(world.gateway.open).toHaveLength(0);
    expect(world.retryButton(A)?.getAttribute('aria-label')).toBe(`Retry ${A}`);
    expect(world.retryButton(B)?.getAttribute('aria-label')).toBe(`Retry ${B}`);
    const before = world.gateway.streams.length;
    await world.press(A, 'Retry');
    expect(world.gateway.streams.slice(before).map(each => each.host)).toEqual([A]);
    expect(world.status(A)).toBe('Connecting');
    expect(world.status(B)).toBe('Unreachable');
    await world.stop();
  });

  test('live, stale, failed, Retry, connecting, live: the last values stay dimmed with their age until the stream returns', async () => {
    const world = rig();
    await world.show([A]);
    await liveWith(world, A, 40);
    const seen: string[] = [world.status(A)];
    for (let loss = 1; loss <= OFFLINE_AFTER; loss += 1) {
      await world.send(A, stream => stream.fail());
      seen.push(world.status(A));
      if (loss < OFFLINE_AFTER) {
        // Between losses the host still shows what it last read, dimmed, with its age.
        expect(world.gauges(A)[0]?.textContent).toBe('CPU40%');
        expect(world.dimmed(A)).toBe(true);
        expect(world.ageText(A)).not.toBeNull();
        await world.advance(backoffMs(loss));
      }
    }
    expect(seen).toEqual(['Online', 'Stale', 'Stale', 'Stale', 'Stale', 'Unreachable']);
    // Failed shows no values and offers Retry.
    expect(world.gauges(A)).toHaveLength(0);
    // Some time on, the host's own slow retry is under way, and still the card says it failed.
    await world.advance(40_000);
    expect(world.status(A)).toBe('Unreachable');
    await world.press(A, 'Retry');
    // The stream is being opened: the last values come back dimmed with their age, and the status
    // says what is happening and not what was.
    expect(world.status(A)).toBe('Connecting');
    expect(world.gauges(A)[0]?.textContent).toBe('CPU40%');
    expect(world.dimmed(A)).toBe(true);
    expect(world.ageText(A)).not.toBeNull();
    expect(world.retryButton(A)).toBeUndefined();
    await world.snapshot(A, {cpuPercent: 9});
    expect(world.status(A)).toBe('Online');
    expect(world.gauges(A)[0]?.textContent).toBe('CPU9%');
    expect(world.dimmed(A)).toBe(false);
    expect(world.ageText(A)).toBeNull();
    await world.stop();
  });

  test('failed, Retry, connecting, and failed again when the host still does not answer', async () => {
    const world = rig();
    await world.show([A]);
    await liveWith(world, A, 40);
    await loseEveryStream(world, A);
    expect(world.status(A)).toBe('Unreachable');
    for (let round = 0; round < 2; round += 1) {
      await world.press(A, 'Retry');
      expect(world.status(A)).toBe('Connecting');
      expect(world.retryButton(A)).toBeUndefined();
      await loseEveryStream(world, A);
      expect(world.status(A)).toBe('Unreachable');
      expect(world.retryButton(A)).toBeDefined();
    }
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
    expect(world.status(B)).toBe('Closed');
    expect(world.retryButton(B)).toBeUndefined();
    expect(world.gateway.open).toHaveLength(0);
    await world.stop();
  });

  test('leaving the page releases every stream and every timer once the linger is over, and opening it opened one stream per host', async () => {
    const world = rig();
    await world.show([A, B]);
    await world.snapshot(A);
    await world.snapshot(B);
    expect(world.gateway.streams.map(each => each.host).sort()).toEqual([A, B]);
    await world.advance(AGE + 1);
    await world.stop();
    await world.advance(SUBSCRIPTION_LINGER_MS - 1);
    expect(world.gateway.open).toHaveLength(2);
    await world.advance(1);
    expect(world.gateway.open).toHaveLength(0);
    expect(world.clock.pending).toBe(0);
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
      expect(world.status(A)).toBe('Stale');
      expect(world.gauges(A)[0]?.textContent).toBe('CPU40%');
      await world.stop();
    }
  });

  test('a check that timed out never makes a delivering host Unreachable: one status, and no Reconnect beside live readings', async () => {
    const world = rig();
    await world.show([A], {kind: 'unavailable', detail: 'timed out'});
    // Before the host has said anything, the check is all there is to go on.
    expect(world.status(A)).toBe('Unreachable');
    expect(world.pressable(A, 'Reconnect')).toBe(true);
    await liveWith(world, A, 40);
    expect(world.status(A)).toBe('Online');
    expect(world.pressable(A, 'Reconnect')).toBe(false);
    expect(world.gauges(A)[0]?.textContent).toBe('CPU40%');
    // A host that stops delivering is no longer vouched for: the check's word and its fix return,
    // over readings that are dimmed.
    await world.advance(AGE + 1);
    expect(world.dimmed(A)).toBe(true);
    expect(world.status(A)).toBe('Unreachable');
    expect(world.pressable(A, 'Reconnect')).toBe(true);
    await world.stop();
  });

  test('when the host stops delivering the summary keeps its place: the live meter and the run count stay, dimmed, with the same numbers', async () => {
    const world = rig(true);
    await world.show([A]);
    await liveWith(world, A, 40);
    await world.runs(A, 3, 4);
    const live = world.summary(A);
    expect(live.cells).toEqual(['16 CPU', '3 live', '7 runs']);
    expect(live.dim).toBe(false);
    // Both hosts' streams lapse; nothing is removed, nothing moves, and nothing is said twice.
    await world.advance(Math.max(AGE, runsTopic.policy.staleAfterMs) + 1);
    const stale = world.summary(A);
    expect(stale.cells).toEqual(live.cells);
    expect(stale.dim).toBe(true);
    // The host answers the next attempt: the same cells, current.
    await world.advance(backoffMs(1));
    await world.runs(A, 3, 4);
    expect(world.summary(A)).toEqual(live);
    await world.stop();
  });

  test('a card’s run count is the runs topic’s, whatever the machine check said', async () => {
    for (const report of [
      {kind: 'ready', facts: {...FACTS, runs: {live: 9, ended_today: 9}}},
      {kind: 'unavailable', detail: 'timed out'},
    ] as const) {
      const world = rig(true);
      await world.show([A], report);
      await liveWith(world, A, 10);
      await world.runs(A, 2, 5);
      expect(world.text(A)).toContain('7 runs');
      expect(world.text(A)).not.toContain('18 runs');
      await world.stop();
    }
  });

  test('the rail reads which hosts deliver from the cards’ own streams, and holds none while it is not watching', async () => {
    const world = rig();
    await world.show([A, B]);
    const watching = world.rail([A, B], true);
    await watching.mount();
    expect(watching.latest()).toEqual([]);
    await world.snapshot(A);
    expect(watching.latest()).toEqual([A]);
    // The store is told each change of who delivers, and nothing in between.
    await world.advance(BEAT);
    await world.send(A, stream => stream.send({case: 'heartbeat', value: {}}));
    expect(watching.reported()).toEqual([[], [A]]);
    // One stream per host serves the card and the rail alike.
    expect(world.gateway.open.map(each => each.host).sort()).toEqual([A, B]);
    await world.advance(AGE + 1);
    expect(watching.latest()).toEqual([]);
    expect(watching.reported()).toEqual([[], [A], []]);
    await watching.unmount();
    await world.stop();
    await world.advance(SUBSCRIPTION_LINGER_MS);
    const idle = world.rail([A, B], false);
    await idle.mount();
    expect(idle.latest()).toEqual([]);
    expect(world.gateway.open).toHaveLength(0);
    await idle.unmount();
  });
});
