/**
 * The hosts feature's pure core: the host panel's store (a host's checkout, the checkout form,
 * sign-in), the "Connect to host" picker, path completion for host paths, and the run inspector
 * with its logs. Its effects reach the main process only through `HostsBridge`, which the app
 * implements with the welcome bridge.
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
} from '@vibesys/hosts';
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
  INSPECTOR_CLOSED,
  type InspectorAction,
  type InspectorCopy,
  type InspectorField,
  type InspectorInspect,
  type InspectorState,
  type InspectorTarget,
  inspectionFields,
  inspectorMachine,
  inspectorView,
} from './inspector.js';
export {
  groupedStreams,
  INSPECTOR_LOGS_CLOSED,
  INSPECTOR_SHEET_CLOSED,
  type InspectorLogsEvent,
  type InspectorLogsInput,
  type InspectorLogsTarget,
  type InspectorLogsView,
  type InspectorSheetAction,
  type InspectorSheetState,
  type InspectorStream,
  inspectorLogsMachine,
  inspectorLogsNotice,
  inspectorLogsView,
  inspectorStreams,
  type LogFollowerView,
  type LogGroup,
  type LogStreamGroup,
  type LogStreamStatus,
  type ShownLine,
} from './inspector-logs.js';
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
