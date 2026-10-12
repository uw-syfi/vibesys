/** Pure control state and presentation decisions for the detached-run inspector sheet. */
import type {HostKey} from '@vibesys/hosts';
import type {WelcomeInspection, WelcomeResult} from '@vibesys/ipc';
import {unprovided} from '@vibesys/machine';
import {assign, type SnapshotFrom, setup} from 'xstate';

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
  | {readonly type: 'copy'; readonly label: string; readonly value: string};

export interface InspectorInspect {
  readonly token: number;
  readonly host: HostKey;
  readonly instance: string;
  readonly includeLog: boolean;
}

export interface InspectorCopy {
  readonly token: number;
  readonly label: string;
  readonly value: string;
}

interface Context {
  readonly view: InspectorState;
  readonly inspection: InspectorInspect | null;
  readonly copy: InspectorCopy | null;
}

function beginLoad(view: InspectorState, includeLog: boolean): Partial<Context> {
  const target = view.target;
  if (target === null) return {};
  const token = view.next;
  const logs = view.includeLog || includeLog;
  return {
    view: {
      ...view,
      next: token + 1,
      includeLog: logs,
      load: {kind: 'loading', token},
      notice: null,
      copying: null,
    },
    inspection: {
      token,
      host: target.host,
      instance: target.instanceId,
      includeLog: logs,
    },
    copy: null,
  };
}

function failed(error: unknown): {readonly kind: 'error'; readonly message: string} {
  return {kind: 'error', message: error instanceof Error ? error.message : String(error)};
}

export const inspectorMachine = setup({
  types: {context: {} as Context, events: {} as InspectorAction},
  actors: {
    inspect: unprovided<WelcomeResult<WelcomeInspection>, InspectorInspect>('inspect'),
    copy: unprovided<WelcomeResult<null>, InspectorCopy>('copy'),
  },
  guards: {
    hasTarget: ({context}) => context.view.target !== null,
    canCopy: ({context}) =>
      context.view.target !== null &&
      context.view.load.kind === 'ready' &&
      context.view.copying === null,
  },
}).createMachine({
  id: 'inspector',
  context: {view: INSPECTOR_CLOSED, inspection: null, copy: null},
  initial: 'closed',
  on: {
    open: {
      target: '.loading',
      reenter: true,
      actions: assign(({context, event}) =>
        beginLoad(
          {
            ...context.view,
            target: event.target,
            includeLog: false,
            load: {kind: 'idle'},
            notice: null,
            copying: null,
          },
          false,
        ),
      ),
    },
    close: {
      target: '.closed',
      actions: assign(({context}) => ({
        view: {...INSPECTOR_CLOSED, next: context.view.next + 1},
        inspection: null,
        copy: null,
      })),
    },
    'load-log': {
      guard: 'hasTarget',
      target: '.loading',
      reenter: true,
      actions: assign(({context}) => beginLoad(context.view, true)),
    },
    refresh: {
      guard: 'hasTarget',
      target: '.loading',
      reenter: true,
      actions: assign(({context}) => beginLoad(context.view, context.view.includeLog)),
    },
    copy: {
      guard: 'canCopy',
      target: '.copying',
      reenter: true,
      actions: assign(({context, event}) => {
        const token = context.view.next;
        return {
          view: {...context.view, next: token + 1, copying: token, notice: null},
          copy: {token, label: event.label, value: event.value},
        };
      }),
    },
  },
  states: {
    closed: {},
    loading: {
      invoke: {
        id: 'inspect',
        src: 'inspect',
        input: ({context}) => context.inspection as InspectorInspect,
        onDone: {
          target: 'shown',
          actions: assign(({context, event}) => ({
            view: {
              ...context.view,
              load: event.output.ok
                ? {kind: 'ready', inspection: event.output.value}
                : {kind: 'error', message: event.output.error},
            },
            inspection: null,
          })),
        },
        onError: {
          target: 'shown',
          actions: assign(({context, event}) => ({
            view: {...context.view, load: failed(event.error)},
            inspection: null,
          })),
        },
      },
    },
    shown: {},
    copying: {
      invoke: {
        id: 'copy',
        src: 'copy',
        input: ({context}) => context.copy as InspectorCopy,
        onDone: {
          target: 'shown',
          actions: assign(({context, event}) => {
            const operation = context.copy as InspectorCopy;
            return {
              view: {
                ...context.view,
                copying: null,
                notice: event.output.ok
                  ? {text: `${operation.label} copied.`, error: false}
                  : {
                      text: `Could not copy ${operation.label.toLowerCase()}: ${event.output.error}`,
                      error: true,
                    },
              },
              copy: null,
            };
          }),
        },
        onError: {
          target: 'shown',
          actions: assign(({context, event}) => {
            const operation = context.copy as InspectorCopy;
            const error = failed(event.error).message;
            return {
              view: {
                ...context.view,
                copying: null,
                notice: {
                  text: `Could not copy ${operation.label.toLowerCase()}: ${error}`,
                  error: true,
                },
              },
              copy: null,
            };
          }),
        },
      },
    },
  },
});

/** What the sheet renders of an inspector snapshot. */
export function inspectorView(snapshot: SnapshotFrom<typeof inspectorMachine>): InspectorState {
  return snapshot.context.view;
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
      value:
        metadata === null && record === null ? 'Unknown' : String(metadata?.pid ?? record?.pid),
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
      value: String(metadata?.protocol_version ?? record?.protocol_version ?? 'Unknown'),
      copy: true,
    },
    {label: 'Control socket', value: inspection.control_socket_path, copy: true},
    {label: 'Server log', value: inspection.server_log_path, copy: true},
  ];
}
