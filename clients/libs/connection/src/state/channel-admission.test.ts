import {describe, expect, test} from 'bun:test';
import {
  type AdmissionEvent,
  type AdmissionOutput,
  type AdmissionState,
  CHANNEL_ROLES,
  type ChannelRole,
  initialAdmissionState,
  poolDiagnostics,
  rolePriority,
  stepAdmission,
} from './channel-admission.js';

const CONFIG = {capacityPerMaster: 5, reservedControlSlots: 2, maxMasters: 3};

function dispatch(
  state: AdmissionState,
  event: AdmissionEvent,
): {state: AdmissionState; outputs: readonly AdmissionOutput[]} {
  return stepAdmission(state, event);
}

function ready(config = CONFIG): AdmissionState {
  return dispatch(initialAdmissionState(config), {type: 'master-ready', masterId: 0}).state;
}

function activateGranted(
  state: AdmissionState,
  outputs: readonly AdmissionOutput[],
): AdmissionState {
  let next = state;
  for (const output of outputs) {
    if (output.type === 'granted') {
      next = dispatch(next, {type: 'activate', leaseId: output.demandId}).state;
    }
  }
  return next;
}

function request(
  state: AdmissionState,
  demandId: number,
  role: ChannelRole,
): {state: AdmissionState; outputs: readonly AdmissionOutput[]} {
  return dispatch(state, {type: 'request', demandId, role});
}

function expectConserved(state: AdmissionState): void {
  const diagnostics = poolDiagnostics(state);
  expect(diagnostics.masterCount).toBeLessThanOrEqual(CONFIG.maxMasters);
  const ids = [
    ...state.demands.map(demand => demand.id),
    ...state.masters.flatMap(master => master.leases.map(lease => lease.id)),
  ];
  expect(new Set(ids).size).toBe(ids.length);
  for (const master of state.masters) {
    expect(master.leases.length).toBeLessThanOrEqual(CONFIG.capacityPerMaster);
    const passive = master.leases.filter(
      lease => lease.role === 'subscribe' || lease.role === 'chat',
    ).length;
    expect(passive).toBeLessThanOrEqual(CONFIG.capacityPerMaster - CONFIG.reservedControlSlots);
    if (master.id !== 0 && master.phase === 'ready' && master.leases.length === 0) {
      expect(state.demands.length).toBeGreaterThan(0);
    }
  }
  const openingSpills = state.masters.filter(
    master => master.id !== 0 && master.phase === 'opening',
  );
  expect(openingSpills.length).toBeLessThanOrEqual(1);
  if (state.phase === 'accepting' && state.demands.length > 0) {
    expect(
      state.masters.some(master => master.phase === 'opening' || master.phase === 'retiring'),
    ).toBe(true);
  }
}

function applyGeneratedEvent(state: AdmissionState, event: AdmissionEvent): AdmissionState {
  const transition = dispatch(state, event);
  expectConserved(transition.state);
  return transition.state;
}

function generatedStep(
  state: AdmissionState,
  nextId: number,
  draw: (bound: number) => number,
): AdmissionState {
  const opening = state.masters.filter(master => master.phase === 'opening');
  const retiring = state.masters.filter(master => master.phase === 'retiring');
  const leases = state.masters.flatMap(master => master.leases);
  const choice = draw(8);
  if (choice <= 2) {
    return applyGeneratedEvent(state, {
      type: 'request',
      demandId: nextId,
      role: CHANNEL_ROLES[draw(CHANNEL_ROLES.length)] as ChannelRole,
    });
  }
  if (choice === 3 && state.demands.length > 0) {
    return applyGeneratedEvent(state, {
      type: 'cancel',
      demandId: state.demands[draw(state.demands.length)]?.id as number,
    });
  }
  if (choice === 4 && opening.length > 0) {
    const master = opening[draw(opening.length)];
    return applyGeneratedEvent(state, {
      type: draw(4) === 0 && master?.id !== 0 ? 'master-failed' : 'master-ready',
      masterId: master?.id as number,
    });
  }
  if (choice === 5 && leases.length > 0) {
    const lease = leases[draw(leases.length)];
    return applyGeneratedEvent(
      state,
      lease?.phase === 'opening'
        ? {type: 'activate', leaseId: lease.id}
        : {type: 'release', leaseId: lease?.id as number},
    );
  }
  if (choice === 6 && leases.length > 0) {
    return applyGeneratedEvent(state, {
      type: 'release',
      leaseId: leases[draw(leases.length)]?.id as number,
    });
  }
  if (retiring.length > 0) {
    const outcomes = ['retired', 'reachable', 'unknown'] as const;
    return applyGeneratedEvent(state, {
      type: 'master-retirement-observed',
      masterId: retiring[draw(retiring.length)]?.id as number,
      outcome: outcomes[draw(outcomes.length)] as (typeof outcomes)[number],
    });
  }
  return state;
}

function runGeneratedSequence(seed: number): void {
  let state = ready();
  let nextId = 1;
  let random = seed;
  const draw = (bound: number): number => {
    random = (random * 1_664_525 + 1_013_904_223) >>> 0;
    return random % bound;
  };
  for (let index = 0; index < 200; index += 1) {
    const beforeOrder = state.nextDemandOrder;
    state = generatedStep(state, nextId, draw);
    if (state.nextDemandOrder > beforeOrder) nextId += 1;
    expectConserved(state);
  }
}

describe('channel admission', () => {
  test('keeps reserved capacity unavailable to passive roles but grants waiting control', () => {
    let state = ready();
    for (let id = 1; id <= 3; id += 1) {
      const admitted = request(state, id, id % 2 === 0 ? 'chat' : 'subscribe');
      expect(admitted.outputs).toEqual([{type: 'granted', demandId: id, masterId: 0}]);
      state = activateGranted(admitted.state, admitted.outputs);
    }
    const passive = request(state, 4, 'chat');
    expect(passive.outputs).toEqual([{type: 'start-master', masterId: 1}]);

    const control = request(passive.state, 5, 'run-control');
    expect(control.outputs).toEqual([{type: 'granted', demandId: 5, masterId: 0}]);
    expect(control.state.demands.map(demand => demand.id)).toEqual([4]);
  });

  test('declares a stable operator-first priority', () => {
    expect([...CHANNEL_ROLES].sort((a, b) => rolePriority(a) - rolePriority(b))).toEqual([
      'host-control',
      'run-control',
      'subscribe',
      'chat',
    ]);
    expect(() => rolePriority('inspect' as ChannelRole)).toThrow('unknown channel role');
  });

  test('retains a creator-less spill master and grants its queued subscriber', () => {
    const config = {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2};
    let state = ready(config);
    const base = request(state, 1, 'chat');
    state = activateGranted(base.state, base.outputs);
    const creator = request(state, 2, 'chat');
    expect(creator.outputs).toEqual([{type: 'start-master', masterId: 1}]);
    const subscriber = request(creator.state, 3, 'subscribe');
    expect(subscriber.outputs).toEqual([]);

    const cancelled = dispatch(subscriber.state, {type: 'cancel', demandId: 2});
    expect(cancelled.outputs).toEqual([{type: 'cancelled', demandId: 2}]);
    const completed = dispatch(cancelled.state, {type: 'master-ready', masterId: 1});

    expect(completed.outputs).toEqual([{type: 'granted', demandId: 3, masterId: 1}]);
    expect(completed.state.masters.find(master => master.id === 1)).toMatchObject({
      phase: 'ready',
      leases: [{id: 3, role: 'subscribe', phase: 'opening'}],
    });
  });

  test('grants higher-priority queued demand before the spill creator', () => {
    const config = {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2};
    let state = ready(config);
    const base = request(state, 1, 'chat');
    state = activateGranted(base.state, base.outputs);
    const chat = request(state, 2, 'chat');
    const subscriber = request(chat.state, 3, 'subscribe');

    const completed = dispatch(subscriber.state, {type: 'master-ready', masterId: 1});

    expect(completed.outputs).toEqual([
      {type: 'granted', demandId: 3, masterId: 1},
      {type: 'capacity', demandId: 2},
    ]);
    expect(completed.state.demands).toEqual([]);
  });

  test('uses FIFO as the tie-break within one role', () => {
    const config = {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2};
    let state = ready(config);
    const base = request(state, 1, 'chat');
    state = activateGranted(base.state, base.outputs);
    const first = request(state, 2, 'subscribe');
    const second = request(first.state, 3, 'subscribe');

    const completed = dispatch(second.state, {type: 'master-ready', masterId: 1});

    expect(completed.outputs).toEqual([
      {type: 'granted', demandId: 2, masterId: 1},
      {type: 'capacity', demandId: 3},
    ]);
  });

  test('failure and close terminally account for every queued demand', () => {
    const config = {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2};
    let state = ready(config);
    const base = request(state, 1, 'chat');
    state = activateGranted(base.state, base.outputs);
    const chat = request(state, 2, 'chat');
    const subscriber = request(chat.state, 3, 'subscribe');
    const failed = dispatch(subscriber.state, {type: 'master-failed', masterId: 1});
    expect(failed.outputs).toEqual([
      {type: 'capacity', demandId: 3},
      {type: 'capacity', demandId: 2},
      {type: 'stop-master', masterId: 1},
    ]);
    expect(failed.state.demands).toEqual([]);
    expect(failed.state.masters.find(master => master.id === 1)?.phase).toBe('retiring');

    const retry = request(failed.state, 4, 'chat');
    const waiting = request(retry.state, 5, 'subscribe');
    const closed = dispatch(waiting.state, {type: 'close'});
    expect(closed.outputs).toEqual([
      {type: 'closed', demandId: 5},
      {type: 'closed', demandId: 4},
    ]);
    expect(closed.state.demands).toEqual([]);
  });

  test('retains a reachable failed retirement for reuse without exceeding the master bound', () => {
    const config = {capacityPerMaster: 2, reservedControlSlots: 1, maxMasters: 2};
    let state = ready(config);
    const base = request(state, 1, 'chat');
    state = activateGranted(base.state, base.outputs);
    const spill = request(state, 2, 'chat');
    const opened = dispatch(spill.state, {type: 'master-ready', masterId: 1});
    state = activateGranted(opened.state, opened.outputs);

    const idle = dispatch(state, {type: 'release', leaseId: 2});
    expect(idle.outputs).toEqual([{type: 'stop-master', masterId: 1}]);
    const retained = dispatch(idle.state, {
      type: 'master-retirement-observed',
      masterId: 1,
      outcome: 'reachable',
    });
    expect(retained.outputs).toEqual([]);
    expect(retained.state.masters.find(master => master.id === 1)?.phase).toBe('retained');

    const reused = request(retained.state, 3, 'chat');
    expect(reused.outputs).toEqual([{type: 'granted', demandId: 3, masterId: 1}]);
    expect(reused.state.masters.find(master => master.id === 1)?.phase).toBe('ready');
    const stoppedAgain = dispatch(reused.state, {type: 'release', leaseId: 3});
    const unresolved = dispatch(stoppedAgain.state, {
      type: 'master-retirement-observed',
      masterId: 1,
      outcome: 'unknown',
    });
    expect(unresolved.state.masters.find(master => master.id === 1)?.phase).toBe('unresolved');

    const exhausted = request(unresolved.state, 4, 'chat');
    expect(exhausted.outputs).toEqual([{type: 'capacity', demandId: 4}]);
    expect(exhausted.state.masters).toHaveLength(2);
  });

  test('close retires base and spills once and preserves every unresolved outcome', () => {
    const config = {capacityPerMaster: 1, reservedControlSlots: 0, maxMasters: 3};
    let state = ready(config);
    for (let id = 1; id <= 3; id += 1) {
      const admitted = request(state, id, 'chat');
      state = admitted.state;
      const starting = admitted.outputs.find(output => output.type === 'start-master');
      if (starting !== undefined) {
        const opened = dispatch(state, {type: 'master-ready', masterId: starting.masterId});
        state = opened.state;
        state = activateGranted(state, opened.outputs);
      } else {
        state = activateGranted(state, admitted.outputs);
      }
    }

    const closed = dispatch(state, {type: 'close'});
    expect(closed.outputs).toEqual([]);
    const baseIdle = dispatch(closed.state, {type: 'release', leaseId: 1});
    const firstIdle = dispatch(baseIdle.state, {type: 'release', leaseId: 2});
    const secondIdle = dispatch(firstIdle.state, {type: 'release', leaseId: 3});
    expect([...baseIdle.outputs, ...firstIdle.outputs, ...secondIdle.outputs]).toEqual([
      {type: 'stop-master', masterId: 0},
      {type: 'stop-master', masterId: 1},
      {type: 'stop-master', masterId: 2},
    ]);

    const baseGone = dispatch(secondIdle.state, {
      type: 'master-retirement-observed',
      masterId: 0,
      outcome: 'retired',
    });
    const firstFailed = dispatch(baseGone.state, {
      type: 'master-retirement-observed',
      masterId: 1,
      outcome: 'reachable',
    });
    const secondUnknown = dispatch(firstFailed.state, {
      type: 'master-retirement-observed',
      masterId: 2,
      outcome: 'unknown',
    });

    expect(secondUnknown.outputs).toEqual([]);
    expect(secondUnknown.state.masters.map(master => [master.id, master.phase])).toEqual([
      [1, 'unresolved'],
      [2, 'unresolved'],
    ]);
    expect(poolDiagnostics(secondUnknown.state).masterCount).toBe(2);
  });

  test('generated request, cancellation, lifecycle, and release sequences conserve state', () => {
    for (let seed = 1; seed <= 100; seed += 1) runGeneratedSequence(seed);
  });
});
