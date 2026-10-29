/**
 * The machine-scoped Settings store's shell: its requests go to the welcome bridge, and each reply
 * is dispatched as the action that names its token, so the reducer decides what a late one means.
 */
import {
  attempt,
  type FailureCause,
  type MachineSettingsWelcomeBridge,
  type WelcomeResult,
} from '@vibesys/ipc';
import {createStore, type Store} from '@vibesys/store';
import {
  INITIAL_MACHINE_SETTINGS,
  type MachineSettingsAction,
  type MachineSettingsCall,
  type MachineSettingsState,
  machineSettingsStep,
} from '../../state/index.js';

type Outcome<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly cause?: FailureCause};

function outcome<T>(result: WelcomeResult<T>): Outcome<T> {
  if (result.ok) return {ok: true, value: result.value};
  return {
    ok: false,
    error: result.error,
    ...(result.cause === undefined ? {} : {cause: result.cause}),
  };
}

/** What `call` answered, as the action that names its token. */
async function reply(
  bridge: MachineSettingsWelcomeBridge,
  call: MachineSettingsCall,
): Promise<MachineSettingsAction> {
  const {host, token} = call;
  switch (call.kind) {
    case 'compute':
      return {
        type: 'computed',
        token,
        host,
        result: outcome(await attempt(() => bridge.compute(host))),
      };
    case 'logins':
      return {
        type: 'logins-read',
        token,
        host,
        result: outcome(await attempt(() => bridge.agentLogins(host))),
      };
    case 'test':
      return {
        type: 'tested',
        token,
        host,
        result: outcome(await attempt(() => bridge.testCluster(host, call.cluster))),
      };
    case 'save':
      return {
        type: 'saved',
        token,
        host,
        result: outcome(await attempt(() => bridge.addCluster(host, call.cluster))),
      };
    case 'remove':
      return {
        type: 'removed',
        token,
        host,
        result: outcome(await attempt(() => bridge.removeCluster(host, call.name))),
      };
    case 'start-login':
      return {
        type: 'signed-in',
        token,
        host,
        provider: call.provider,
        result: outcome(await attempt(() => bridge.startLogin(host, call.provider))),
      };
  }
}

/** The machine-scoped Settings store over `bridge`; a machine is read when it is first shown. */
export function createMachineSettingsStore(
  bridge: MachineSettingsWelcomeBridge,
): Store<MachineSettingsState, MachineSettingsAction> {
  return createStore(machineSettingsStep, INITIAL_MACHINE_SETTINGS, (call, dispatch) => {
    void reply(bridge, call).then(dispatch);
  });
}
