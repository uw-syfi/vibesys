/**
 * The Machines screen's shell: its store, whose requests go to the welcome bridge. Each reply is
 * dispatched as the action that names its token, so the reducer decides what a late one means.
 * Adding a machine or changing its checkout is the hosts feature's checked `setCheckout`, named by
 * the request's token so leaving the form can cancel it; path listings are its `complete`.
 */
import {
  attempt,
  type HostsWelcomeBridge,
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
  Pick<HostsWelcomeBridge, 'setCheckout' | 'cancelCheckout' | 'signIn' | 'complete'>;

type Outcome<T> =
  | {readonly ok: true; readonly value: T}
  | {readonly ok: false; readonly error: string};

function outcome<T>(result: WelcomeResult<T>): Outcome<T> {
  return result.ok ? {ok: true, value: result.value} : {ok: false, error: result.error};
}

/** Make `request`; settles once its reply is dispatched. */
function perform(
  bridge: MachinesBridge,
  request: MachinesRequest,
  dispatch: (action: MachinesAction) => void,
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

/** The Machines store over `bridge`: it lists the machines at once and checks each one. */
export function createMachinesStore(bridge: MachinesBridge): Store<MachinesState, MachinesAction> {
  const initial = initialMachines();
  const store = createStore(machinesStep, initial.state, (request, dispatch) => {
    void perform(bridge, request, dispatch);
  });
  for (const request of initial.requests) void perform(bridge, request, store.dispatch);
  return store;
}
