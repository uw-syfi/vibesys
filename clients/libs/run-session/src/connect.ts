/**
 * The run connection over a control transport: the run's snapshot request and its persistent event
 * stream, placed by the stream reconciler. Runtime-neutral: the app supplies the transport (a
 * WebSocket, or the desktop shell's bridge shaped as one).
 */
import {ServerError} from '@vibesys/contracts';
import {
  type ControlChannelState,
  type ControlTransport,
  closeStreamAndTransport,
  PersistentEventStream,
  type PersistentEventStreamOptions,
  type ScheduleTimeout,
  StreamReconciler,
  sameControlChannelState,
} from '@vibesys/transport';
import {isIterationStoreReplaced, isLegacyUnsupportedRequest} from './connection.js';
import type {
  ChatResponse,
  RunCommand,
  RunCommandOutcome,
  RunConnection,
  TransportHooks,
  WebChatOptions,
} from './ports.js';

/** The event stream's tail and its redial timing; the defaults are the transport's. */
export interface RunConnectionOptions {
  readonly tail?: number;
  readonly scheduleTimeout?: ScheduleTimeout;
  readonly reconnectDelaysMs?: readonly number[];
}

/**
 * How a session opens its run: the transport `open` builds (reporting to the session's hooks, with
 * repeats of one control-channel state dropped), and the run connection over it.
 */
export function connectRun(
  open: (hooks: TransportHooks) => ControlTransport,
  options: RunConnectionOptions = {},
): (hooks: TransportHooks) => RunConnection {
  return hooks => {
    let last: ControlChannelState = {status: 'connected'};
    const transport = open({
      onConnectionState: state => {
        if (sameControlChannelState(last, state)) return;
        last = state;
        hooks.onConnectionState(state);
      },
    });
    return openRunConnection(transport, options);
  };
}

function openRunConnection(
  transport: ControlTransport,
  options: RunConnectionOptions = {},
): RunConnection {
  const {tail, reconnectDelaysMs, scheduleTimeout} = options;
  const streamOptions: PersistentEventStreamOptions = {
    ...(tail === undefined ? {} : {tail}),
    ...(reconnectDelaysMs === undefined ? {} : {reconnectDelaysMs}),
    ...(scheduleTimeout === undefined ? {} : {scheduleTimeout}),
  };
  const stream = new PersistentEventStream(transport, streamOptions);
  let closing: Promise<void> | null = null;
  // The browser replays the whole log and exposes no backfill action, so the positive chunk is
  // dormant; it follows the configured tail so a future history action cannot drift from it.
  const reconciler = new StreamReconciler({backfillChunk: tail ?? 1});
  let iterationCapability: 'unknown' | 'supported' | 'unsupported' = 'unknown';
  return {
    async loadSnapshot(storeId, signal) {
      const response = await transport.request(
        {
          type: 'query.snapshot',
          ...(storeId === undefined ? {} : {expected_store_id: storeId}),
        },
        {signal},
      );
      if (response.ok !== true || response.snapshot === null || response.snapshot === undefined) {
        throw new ServerError(
          response.error ?? 'Server did not return a run snapshot',
          response.diagnostic ?? null,
        );
      }
      return response.snapshot;
    },
    async loadIterations(cursor, expectedStoreId, signal) {
      if (iterationCapability === 'unsupported') return {kind: 'unsupported'};
      try {
        const response = await transport.request(
          {
            type: 'query.iterations',
            expected_store_id: expectedStoreId,
            ...(cursor === null ? {} : {cursor}),
          },
          {signal},
        );
        iterationCapability = 'supported';
        if (response.iterations_ready === false || response.iterations == null) {
          return {kind: 'not-ready'};
        }
        return {kind: 'result', result: response.iterations};
      } catch (error) {
        if (
          iterationCapability === 'unknown' &&
          isLegacyUnsupportedRequest(error) &&
          !isIterationStoreReplaced(error)
        ) {
          iterationCapability = 'unsupported';
          return {kind: 'unsupported'};
        }
        throw error;
      }
    },
    ask: (text, options = {}) => askRun(transport, text, options),
    async createChatThread(harness, options = {}) {
      const response = await transport.request(
        {type: 'query.chat_thread_create', ...(harness === null ? {} : {harness})},
        options.signal === undefined ? undefined : {signal: options.signal},
      );
      if (response.ok !== true || response.chat_thread == null) {
        throw new ServerError(
          response.error ?? 'Server did not return the new chat thread',
          response.diagnostic ?? null,
        );
      }
      return {thread: response.chat_thread, events: response.events ?? []};
    },
    command: input => sendCommand(transport, input),
    subscribe(handlers) {
      return stream.subscribe({
        cursor: handlers.cursor,
        storeId: () => reconciler.storeId(),
        shouldReconnect: handlers.shouldReconnect,
        onMessage: (message, context) => {
          if (message.type === 'event_batch') {
            handlers.onBatch(message, reconciler.reconcileBatch(message, context), context);
          } else if (message.type === 'protocol_error') {
            handlers.onFault(new ServerError(message.message, message.diagnostic ?? null));
          }
        },
        onConnectionState: handlers.onConnectionState,
      });
    },
    retry: () => stream.retry(),
    retryIfDisconnected: () => stream.retryIfDisconnected(),
    rebootstrap: () => stream.rebootstrap(),
    redialControls: () => transport.reconnect(),
    close() {
      closing ??= closeStreamAndTransport(stream, transport);
      return closing;
    },
  };
}

/** Deliver one command and say what the server did with it, with its typed reason when it refused. */
async function sendCommand(
  transport: ControlTransport,
  input: RunCommand,
): Promise<RunCommandOutcome> {
  const response = await transport.request(input);
  const acknowledgement = response.ack;
  if (response.ok !== true || acknowledgement === null || acknowledgement === undefined) {
    return {
      kind: 'rejected',
      message: response.error ?? null,
      code: response.diagnostic?.code ?? null,
    };
  }
  return {kind: 'acknowledged', action: acknowledgement.action, status: acknowledgement.status};
}

async function askRun(
  transport: ControlTransport,
  text: string,
  options: WebChatOptions,
): Promise<ChatResponse> {
  const response = await transport.request(
    {
      type: 'query.chat',
      text,
      ...(options.threadId === undefined ? {} : {thread_id: options.threadId}),
    },
    options.signal === undefined ? undefined : {signal: options.signal},
  );
  if (response.ok !== true || response.chat == null) {
    throw new ServerError(
      response.error ?? 'Server did not return a chat answer',
      response.diagnostic ?? null,
    );
  }
  return {chat: response.chat, events: response.events ?? []};
}
