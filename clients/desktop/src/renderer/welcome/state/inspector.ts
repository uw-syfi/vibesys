/** Pure state and presentation decisions for the detached-run inspector sheet. */
import {only, type Step} from '@vibesys/core-state';
import type {
  WelcomeInspection,
  WelcomeResult,
} from '../../../common/welcome-protocol.js';
import type {HostKey} from '../../../common/hosts.js';

export interface InspectorTarget {
  readonly host: HostKey;
  readonly hostLabel: string;
  readonly instanceId: string;
  readonly task: string | null;
  readonly runId: string | null;
  readonly project: string;
  /** Stable region to focus if a list redraw removes the button that opened the sheet. */
  readonly focusFallbackId: 'title-strip' | 'recent-section' | 'host-panel';
}

type InspectorLoad =
  | {readonly kind: 'idle'}
  | {readonly kind: 'loading'; readonly token: number}
  | {readonly kind: 'ready'; readonly inspection: WelcomeInspection}
  | {readonly kind: 'error'; readonly message: string};

export interface InspectorState {
  readonly next: number;
  readonly target: InspectorTarget | null;
  /** Once requested, refresh continues returning one coherent observation with diagnostics. */
  readonly includeLog: boolean;
  readonly load: InspectorLoad;
  readonly notice: {readonly text: string; readonly error: boolean} | null;
  readonly copying: number | null;
}

export const INSPECTOR_CLOSED: InspectorState = {
  next: 1,
  target: null,
  includeLog: false,
  load: {kind: 'idle'},
  notice: null,
  copying: null,
};

export type InspectorAction =
  | {readonly type: 'open'; readonly target: InspectorTarget}
  | {readonly type: 'close'}
  | {readonly type: 'load-log'}
  | {readonly type: 'refresh'}
  | {
      readonly type: 'inspection-reply';
      readonly token: number;
      readonly result: WelcomeResult<WelcomeInspection>;
    }
  | {readonly type: 'copy'; readonly label: string; readonly value: string}
  | {
      readonly type: 'copy-reply';
      readonly token: number;
      readonly label: string;
      readonly error: string | null;
    };

export type InspectorRequest =
  | {
      readonly kind: 'inspect';
      readonly token: number;
      readonly host: HostKey;
      readonly instance: string;
      readonly includeLog: boolean;
    }
  | {readonly kind: 'copy'; readonly token: number; readonly label: string; readonly value: string};

function load(state: InspectorState, includeLog: boolean): Step<InspectorState, InspectorRequest> {
  const target = state.target;
  if (target === null) return only(state);
  const token = state.next;
  const logs = state.includeLog || includeLog;
  return {
    state: {...state, next: token + 1, includeLog: logs, load: {kind: 'loading', token}},
    requests: [
      {
        kind: 'inspect',
        token,
        host: target.host,
        instance: target.instanceId,
        includeLog: logs,
      },
    ],
  };
}

export function inspectorStep(
  state: InspectorState,
  action: InspectorAction,
): Step<InspectorState, InspectorRequest> {
  switch (action.type) {
    case 'open':
      return load(
        {
          ...state,
          target: action.target,
          includeLog: false,
          load: {kind: 'idle'},
          notice: null,
          copying: null,
        },
        false,
      );
    case 'close':
      return only({...INSPECTOR_CLOSED, next: state.next + 1});
    case 'load-log':
      return load(state, true);
    case 'refresh':
      return load(state, state.includeLog);
    case 'inspection-reply':
      if (
        state.target === null ||
        state.load.kind !== 'loading' ||
        state.load.token !== action.token
      ) {
        return only(state);
      }
      return only({
        ...state,
        load: action.result.ok
          ? {kind: 'ready', inspection: action.result.value}
          : {kind: 'error', message: action.result.error},
      });
    case 'copy': {
      if (state.target === null) return only(state);
      const token = state.next;
      return {
        state: {...state, next: token + 1, copying: token, notice: null},
        requests: [{kind: 'copy', token, label: action.label, value: action.value}],
      };
    }
    case 'copy-reply':
      if (state.copying !== action.token) return only(state);
      return only({
        ...state,
        copying: null,
        notice:
          action.error === null
            ? {text: `${action.label} copied.`, error: false}
            : {text: `Could not copy ${action.label.toLowerCase()}: ${action.error}`, error: true},
      });
  }
}

export interface InspectorField {
  readonly label: string;
  readonly value: string;
  readonly copy: boolean;
}

/** Visible metadata, preferring the retained snapshot and using recent context when it ended. */
export function inspectionFields(
  target: InspectorTarget,
  inspection: WelcomeInspection,
): InspectorField[] {
  const record = inspection.record;
  const metadata = inspection.metadata;
  const value = (
    primary: string | null | undefined,
    fallback: string | null | undefined = 'Unknown',
  ): string =>
    primary === null || primary === undefined || primary === '' ? (fallback ?? 'Unknown') : primary;
  return [
    {label: 'Host', value: target.hostLabel, copy: false},
    {label: 'Hostname', value: value(metadata?.hostname, record?.hostname), copy: true},
    {label: 'Task', value: value(metadata?.task, target.task), copy: true},
    {label: 'Run', value: value(metadata?.run_id, record?.run_id ?? target.runId), copy: true},
    {label: 'Instance', value: inspection.id, copy: true},
    {
      label: 'Project',
      value: value(metadata?.project_root, record?.project_root ?? target.project),
      copy: true,
    },
    {
      label: 'VibeSys checkout',
      value: value(metadata?.vibesys_root, record?.vibesys_root),
      copy: true,
    },
    {label: 'State', value: inspection.state, copy: false},
    {
      label: 'PID',
      value: metadata === null && record === null ? 'Unknown' : String(metadata?.pid ?? record?.pid),
      copy: true,
    },
    {
      label: 'Started',
      value:
        metadata === null && record === null
          ? 'Unknown'
          : new Date((metadata?.started_at ?? record?.started_at ?? 0) * 1000).toISOString(),
      copy: false,
    },
    {
      label: 'VibeSys version',
      value: value(metadata?.vibesys_version, record?.vibesys_version),
      copy: true,
    },
    {
      label: 'Protocol version',
      value: String(
        metadata?.protocol_version ?? record?.protocol_version ?? 'Unknown',
      ),
      copy: true,
    },
    {label: 'Control socket', value: inspection.control_socket_path, copy: true},
    {label: 'Server log', value: inspection.server_log_path, copy: true},
  ];
}
