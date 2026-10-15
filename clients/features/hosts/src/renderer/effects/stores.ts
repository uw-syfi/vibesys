/**
 * The hosts feature's shell: its stores, each request performed through the injected
 * `HostsBridge` and its reply dispatched back with the request's token. Nothing here polls: a
 * host's runs arrive as pushes the app routes elsewhere.
 */

import type {HostKey} from '@vibesys/hosts';
import {settled, type WelcomeInspection, type WelcomeResult} from '@vibesys/ipc';
import {
  type LogListOutcome,
  type LogReadInput,
  type LogReadOutcome,
  type LogTransport,
  logFollowMachineFor,
} from '@vibesys/logs';
import {clockFrom, type ScheduleTimeout} from '@vibesys/machine';
import {createStore, type Store} from '@vibesys/store';
import {type AnyActorRef, createActor, fromPromise, type Subscription} from 'xstate';
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionField,
  type CompletionState,
  completionStep,
  type HostAction,
  type HostState,
  type HostsBridge,
  hostInitial,
  hostStep,
  type InspectorCopy,
  type InspectorInspect,
  type InspectorLogsEvent,
  type InspectorLogsTarget,
  type InspectorSheetAction,
  type InspectorSheetState,
  inspectorLogsMachine,
  inspectorLogsView,
  inspectorMachine,
  inspectorView,
  type LogFollowerView,
  PICKER_CLOSED,
  type PickerAction,
  type PickerState,
  pickerStep,
} from '../../state/index.js';

function inspect(
  bridge: HostsBridge,
  request: InspectorInspect,
  signal: AbortSignal,
): Promise<WelcomeResult<WelcomeInspection>> {
  let pending = true;
  const cancel = (): void => {
    if (pending) void settled(bridge.cancelInspection(request.token));
  };
  signal.addEventListener('abort', cancel, {once: true});
  let answer: Promise<WelcomeResult<WelcomeInspection>>;
  try {
    answer = bridge.inspect(request.host, request.instance, request.token);
  } catch (error) {
    answer = Promise.reject(error);
  }
  return settled(answer).finally(() => {
    pending = false;
    signal.removeEventListener('abort', cancel);
  });
}

async function copyValue(
  copy: (value: string) => Promise<void>,
  request: InspectorCopy,
): Promise<WelcomeResult<null>> {
  try {
    await copy(request.value);
    return {ok: true, value: null};
  } catch (error) {
    return {
      ok: false,
      error: error instanceof Error ? error.message : String(error),
      authNeeded: false,
    };
  }
}

type LogsAnswer = LogListOutcome | LogReadOutcome;

function isOutcome(value: unknown): value is LogsAnswer {
  return (
    typeof value === 'object' && value !== null && typeof Reflect.get(value, 'ok') === 'boolean'
  );
}

/** The last read token this document named; each read takes the next, so none repeats here. */
let lastLogRead = 0;

function nextLogReadToken(): string {
  lastLogRead += 1;
  return `l${lastLogRead}`;
}

/** Call `ask` and settle its answer, a synchronous throw included, as a `WelcomeResult`. */
function settledCall<T>(ask: () => Promise<WelcomeResult<T>>): Promise<WelcomeResult<T>> {
  return settled(new Promise<WelcomeResult<T>>(answer => answer(ask())));
}

/**
 * Ask the main process one logs question about `target`, resolving a refusal as a `transport`
 * failure. An IPC request cannot be withdrawn itself, so an aborted request rejects at once, its
 * late answer is dropped, and `withdraw` (a read's) tells the main process to abort its read.
 */
function askLogs<T extends LogsAnswer>(
  ask: () => Promise<WelcomeResult<LogsAnswer | null>>,
  signal: AbortSignal,
  withdraw?: () => void,
): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const aborted = (): void => {
      reject(signal.reason);
      withdraw?.();
    };
    if (signal.aborted) {
      aborted();
      return;
    }
    signal.addEventListener('abort', aborted, {once: true});
    const failed = (message: string): T =>
      ({ok: false, failure: {code: 'transport', message}}) as T;
    void settledCall(ask).then(result => {
      signal.removeEventListener('abort', aborted);
      if (!result.ok) resolve(failed(result.error));
      else resolve(isOutcome(result.value) ? (result.value as T) : failed('no log answer'));
    });
  });
}

/**
 * The followers' reads over `bridge`, each to the host of the run on screen (`host`), named by a
 * token so that aborting one withdraws it in the main process too.
 */
function logTransport(bridge: HostsBridge, host: () => HostKey | undefined): LogTransport {
  return {
    read: (input: LogReadInput, signal) => {
      const on = host();
      if (on === undefined) return Promise.reject(new Error('no run is inspected'));
      const token = nextLogReadToken();
      return askLogs<LogReadOutcome>(
        () => bridge.logs(on, input.instance_id, {kind: 'read', token, read: input}),
        signal as AbortSignal,
        () => void settledCall(() => bridge.logs(on, input.instance_id, {kind: 'withdraw', token})),
      );
    },
  };
}

function followerView(ref: AnyActorRef | undefined): LogFollowerView | null {
  if (ref === undefined) return null;
  const {context} = ref.getSnapshot() as {context: LogFollowerView};
  return {view: context.view, failure: context.failure};
}

/**
 * The detached-run inspector store over the host bridge, an injected clipboard writer, and the
 * clock its logs retry and follow on. Opening the sheet also opens its logs, `refresh` re-lists
 * them, and closing it stops every log read and lets the host's logs channel go.
 */
export function createInspectorStore(
  bridge: HostsBridge,
  copy: (value: string) => Promise<void>,
  schedule: ScheduleTimeout,
): Store<InspectorSheetState, InspectorSheetAction> {
  const machine = inspectorMachine.provide({
    actors: {
      inspect: fromPromise<WelcomeResult<WelcomeInspection>, InspectorInspect>(({input, signal}) =>
        inspect(bridge, input, signal),
      ),
      copy: fromPromise<WelcomeResult<null>, InspectorCopy>(({input}) => copyValue(copy, input)),
    },
  });
  // A read goes to the host of the run on screen: opening another run restarts the follower.
  let target: InspectorLogsTarget | null = null;
  const transport = logTransport(bridge, () => target?.host);
  const logsMachine = inspectorLogsMachine.provide({
    actors: {
      list: fromPromise<LogListOutcome, InspectorLogsTarget>(({input, signal}) =>
        askLogs<LogListOutcome>(
          () => bridge.logs(input.host, input.instanceId, {kind: 'list'}),
          signal,
        ),
      ),
      follow: logFollowMachineFor(transport),
    },
  });
  // Both actors live as long as the window that renders them, so they are never stopped.
  const inspector = createActor(machine).start();
  const logs = createActor(logsMachine, {input: {}, clock: clockFrom(schedule)});
  const listeners = new Set<() => void>();
  let follower: AnyActorRef | undefined;
  let followerSubscription: Subscription | null = null;
  let seen: readonly unknown[] = [];
  let state: InspectorSheetState = {
    ...inspectorView(inspector.getSnapshot()),
    logs: inspectorLogsView(logs.getSnapshot(), null),
  };

  // Publish only a change: an actor notifies on every event, also one that changed nothing.
  const publish = (): void => {
    const now = [inspector.getSnapshot(), logs.getSnapshot(), follower?.getSnapshot()];
    if (now.every((snapshot, index) => snapshot === seen[index])) return;
    seen = now;
    state = {
      ...inspectorView(inspector.getSnapshot()),
      logs: inspectorLogsView(logs.getSnapshot(), followerView(follower)),
    };
    for (const listener of [...listeners]) listener();
  };
  inspector.subscribe(publish);
  logs.subscribe(snapshot => {
    const child = snapshot.children['follow'] as AnyActorRef | undefined;
    if (child !== follower) {
      followerSubscription?.unsubscribe();
      follower = child;
      followerSubscription = child?.subscribe(publish) ?? null;
    }
    publish();
  });
  logs.start();

  const sendLogs = (event: InspectorLogsEvent): void => logs.send(event);
  return {
    getState: () => state,
    subscribe: listener => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    dispatch: action => {
      switch (action.type) {
        case 'select-log':
          sendLogs({type: 'select', streamId: action.streamId});
          return;
        case 'open':
          inspector.send(action);
          // Set before the logs open, so the follower's first read already has its host.
          target = {host: action.target.host, instanceId: action.target.instanceId};
          sendLogs({type: 'open', target});
          return;
        case 'close':
          inspector.send(action);
          sendLogs({type: 'close'});
          target = null;
          void settled(bridge.releaseLogs());
          return;
        case 'refresh':
          inspector.send(action);
          sendLogs({type: 'refresh'});
          return;
        case 'copy':
          inspector.send(action);
          return;
      }
    },
  };
}

/** The host panel's store, showing `firstHost` until told otherwise. */
export function createHostStore(
  bridge: HostsBridge,
  firstHost: HostKey,
): Store<HostState, HostAction> {
  return createStore(hostStep, hostInitial(firstHost), (request, dispatch) => {
    switch (request.kind) {
      case 'host':
        void settled(bridge.host(request.host)).then(result =>
          dispatch({type: 'host-reply', token: request.token, result}),
        );
        return;
      case 'set-checkout':
        void settled(bridge.setCheckout(request.host, request.path, request.token)).then(result =>
          dispatch({type: 'checkout-reply', token: request.token, result}),
        );
        return;
      case 'cancel-checkout':
        // The cancelled change's own reply is stale; cancellation only revokes its main-process work.
        void settled(bridge.cancelCheckout(request.token));
        return;
      case 'sign-in':
        void settled(bridge.signIn(request.host)).then(result =>
          dispatch({type: 'sign-in-reply', token: request.token, result}),
        );
        return;
    }
  });
}

/** The "Connect to host" picker's store; choosing a host calls `showHost`. */
export function createPickerStore(
  bridge: HostsBridge,
  showHost: (host: HostKey) => void,
): Store<PickerState, PickerAction> {
  return createStore(pickerStep, PICKER_CLOSED, (request, dispatch) => {
    switch (request.kind) {
      case 'aliases':
        void settled(bridge.hosts()).then(result =>
          dispatch({type: 'aliases-reply', token: request.token, result}),
        );
        return;
      case 'show-host':
        showHost(request.host);
        return;
    }
  });
}

/** Completion listings, each asked after a pause; a newer request for a field replaces it. */
export function createCompletionStore(
  bridge: HostsBridge,
): Store<CompletionState, CompletionAction> {
  const pending = new Map<CompletionField, ReturnType<typeof setTimeout>>();
  return createStore(completionStep, COMPLETION_EMPTY, (request, dispatch) => {
    clearTimeout(pending.get(request.field));
    pending.set(
      request.field,
      setTimeout(() => {
        pending.delete(request.field);
        void settled(bridge.complete(request.host, request.directory)).then(result =>
          dispatch({type: 'reply', field: request.field, token: request.token, result}),
        );
      }, request.afterMs),
    );
  });
}
