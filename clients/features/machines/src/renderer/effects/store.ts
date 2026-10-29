/**
 * The Machines screen's shell: its store, whose requests go to the welcome bridge. Each reply is
 * dispatched as the action that names its token, so the reducer decides what a late one means.
 * Adding a machine or changing its checkout is the hosts feature's checked `setCheckout`, named by
 * the request's token so leaving the form can cancel it; path listings are its `complete`.
 */
import {
  attempt,
  type FailureCause,
  type HostsWelcomeBridge,
  type MachineSettingsWelcomeBridge,
  type MachinesWelcomeBridge,
  type WelcomeResult,
} from '@vibesys/ipc';
import {createStore, type Store} from '@vibesys/store';
import {
  initialMachines,
  type MachinesAction,
  type MachinesRequest,
  type MachinesState,
  machinesStep,
} from '../../state/index.js';

/** The bridge calls the Machines store uses. */
export type MachinesBridge = MachinesWelcomeBridge &
  Pick<HostsWelcomeBridge, 'setCheckout' | 'cancelCheckout' | 'signIn' | 'complete'> &
  Pick<MachineSettingsWelcomeBridge, 'compute' | 'testCluster'>;

/** The time now in milliseconds, by the shell's clock; it stamps what a test found. */
export type Clock = () => number;

type Outcome<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string; readonly cause?: FailureCause};

/** A reply as the reducer reads it: a failed connection keeps its typed cause beside its text. */
function outcome<T>(result: WelcomeResult<T>): Outcome<T> {
  if (result.ok) return {ok: true, value: result.value};
  return {
    ok: false,
    error: result.error,
    ...(result.cause === undefined ? {} : {cause: result.cause}),
  };
}

/** Make `request`; settles once its reply is dispatched. */
function perform(
  bridge: MachinesBridge,
  request: MachinesRequest,
  dispatch: (action: MachinesAction) => void,
  now: Clock,
): Promise<void> {
  const {token} = request;
  const then = <T>(
    call: () => Promise<WelcomeResult<T>>,
    action: (result: Outcome<T>) => MachinesAction,
  ): Promise<void> => attempt(call).then(result => dispatch(action(outcome(result))));
  switch (request.kind) {
    case 'list':
      return then(
        () => bridge.machines(),
        result => ({type: 'listed', token, result}),
      );
    case 'report':
      return then(
        () => bridge.machine(request.host),
        result => ({
          type: 'reported',
          token,
          host: request.host,
          result,
        }),
      );
    case 'compute':
      return then(
        () => bridge.compute(request.host),
        result => ({type: 'computed', token, host: request.host, result}),
      );
    case 'test-cluster':
      return then(
        () => bridge.testCluster(request.host, request.cluster),
        result => ({
          type: 'tested',
          token,
          host: request.host,
          name: request.cluster.name,
          at: now(),
          result,
        }),
      );
    case 'complete':
      return then(
        () => bridge.complete(request.host, request.directory),
        result => ({
          type: 'completed',
          form: request.form,
          token,
          result,
        }),
      );
    case 'inspect':
      return then(
        () => bridge.inspectCheckout(request.host, request.path),
        result => ({
          type: 'inspected',
          form: request.form,
          path: request.path,
          token,
          result,
        }),
      );
    case 'save':
      return then(
        () => bridge.setCheckout(request.host, request.checkout, token),
        result => ({
          type: 'saved',
          form: request.form,
          token,
          result,
        }),
      );
    case 'cancel-save':
      return attempt(() => bridge.cancelCheckout(token)).then(() => undefined);
    case 'remove':
      return then(
        () => bridge.removeMachine(request.host),
        result => ({type: 'done', token, result}),
      );
    case 'sign-in':
      return then(
        () => bridge.signIn(request.host),
        result => ({type: 'done', token, result}),
      );
  }
}

/**
 * The Machines store over `bridge`: it lists the machines at once and checks each one. `now` is
 * the clock a cluster test's time is read from.
 */
export function createMachinesStore(
  bridge: MachinesBridge,
  now: Clock = () => Date.now(),
): Store<MachinesState, MachinesAction> {
  const initial = initialMachines();
  const store = createStore(machinesStep, initial.state, (request, dispatch) => {
    void perform(bridge, request, dispatch, now);
  });
  for (const request of initial.requests) void perform(bridge, request, store.dispatch, now);
  return store;
}
