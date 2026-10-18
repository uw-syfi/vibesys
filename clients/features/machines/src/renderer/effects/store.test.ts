import {expect, test} from 'bun:test';
import {UNEXPECTED_FAILURE} from '@vibesys/ipc';
import {createMachinesStore, type MachinesBridge} from './store.js';

async function settle(): Promise<void> {
  for (let turn = 0; turn < 10; turn += 1) await Promise.resolve();
}

/** A bridge whose requests all answer `reply`, except `machines`, which is the given call. */
function bridgeListing(machines: MachinesBridge['machines']): MachinesBridge {
  const answer = () => Promise.resolve({ok: true as const, value: null});
  return {
    machines,
    machine: () =>
      Promise.resolve({ok: true as const, value: {kind: 'ready' as const, facts: null}}),
    inspectCheckout: () =>
      Promise.resolve({ok: true as const, value: {state: 'not_found' as const}}),
    removeMachine: () => Promise.resolve({ok: true as const, value: []}),
    setCheckout: answer as unknown as MachinesBridge['setCheckout'],
    cancelCheckout: answer as unknown as MachinesBridge['cancelCheckout'],
    signIn: answer as unknown as MachinesBridge['signIn'],
    complete: () => Promise.resolve({ok: true as const, value: {kind: 'unsupported' as const}}),
  };
}

test('machines that cannot be listed say something unexpected, not the raw error', async () => {
  const raw = 'bridge.machines is not a function';
  const throwing: MachinesBridge['machines'] = () => {
    throw new TypeError(raw);
  };
  const rejecting: MachinesBridge['machines'] = () => Promise.reject(new Error(raw));
  for (const machines of [throwing, rejecting]) {
    const store = createMachinesStore(bridgeListing(machines));
    await settle();

    expect(store.getState().listProblem).toBe(UNEXPECTED_FAILURE);
  }
});

test('a listing the main process refused keeps its words', async () => {
  const refusing: MachinesBridge['machines'] = () =>
    Promise.resolve({ok: false, error: 'hosts.json is unreadable.', authNeeded: false});
  const store = createMachinesStore(bridgeListing(refusing));
  await settle();

  expect(store.getState().listProblem).toBe('hosts.json is unreadable.');
});
