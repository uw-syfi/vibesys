/**
 * The hosts feature's pure core: the host panel's store (a host's checkout, the checkout form,
 * sign-in), the "Connect to host" picker, and path completion for host paths. Its effects reach
 * the main process only through `HostsBridge`, which the app implements with the welcome bridge.
 */

import type {HostsWelcomeBridge} from '@vibesys/ipc';

/** The welcome bridge's requests the hosts feature makes. */
export type HostsBridge = HostsWelcomeBridge;

export {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionField,
  type CompletionRequest,
  type CompletionState,
  completionStep,
  splitPath,
  suggestions,
} from './completion.js';
export {
  type CheckoutForm,
  type HostAction,
  type HostRequest,
  type HostState,
  hostAwaited,
  hostInitial,
  hostInvariants,
  hostLabelOf,
  hostStep,
} from './host.js';
export {
  filterHosts,
  type HostChoice,
  moveSelection,
  PICKER_CLOSED,
  type PickerAction,
  type PickerRequest,
  type PickerState,
  pickerChoices,
  pickerInvariants,
  pickerStep,
} from './picker.js';
