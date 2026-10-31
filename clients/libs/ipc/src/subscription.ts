/**
 * A window's subscriptions on the wire. The window sends one validated request per port and main
 * answers with the updates to fold, in the same reply envelope as the gateway port; a cancelled or
 * ended port is the end of the subscription. Nothing crosses Electron outside a generated message,
 * so the closed sets (steps, reasons) are the generated enums, mapped to the subscription types by
 * tables the compiler holds total.
 */
import {create, type MessageInitShape, toBinary} from '@bufbuild/protobuf';
import {TimestampSchema, timestampFromMs, timestampMs} from '@bufbuild/protobuf/wkt';
import {type HostKey, hostFromKey} from '@vibesys/hosts';
import {
  DesktopConnectStep,
  DesktopRetryRequestSchema,
  DesktopSubscribeRequestSchema,
  type DesktopSubscriptionControl,
  DesktopSubscriptionReason,
  DesktopSubscriptionRequestSchema,
  DesktopSubscriptionUpdateSchema,
  type SubscribeRequest,
  validatedExactFromBinary,
} from '@vibesys/proto';
import type {
  ConnectStep,
  Control,
  FailureReason,
  SubscriptionPort,
  SubscriptionUpdate,
} from '@vibesys/subscription';
import {decodeGatewayReply, type GatewayPortBridge} from './gateway.js';

export const SUBSCRIPTION_CHANNEL = 'vibesys:subscription';

type Known<E extends number> = Exclude<E, 0>;

const STEPS: Readonly<Record<ConnectStep, Known<DesktopConnectStep>>> = {
  gateway: DesktopConnectStep.GATEWAY,
  handshake: DesktopConnectStep.HANDSHAKE,
  stream: DesktopConnectStep.STREAM,
};

const REASONS: Readonly<Record<FailureReason, Known<DesktopSubscriptionReason>>> = {
  unreachable: DesktopSubscriptionReason.UNREACHABLE,
  ended: DesktopSubscriptionReason.ENDED,
  silent: DesktopSubscriptionReason.SILENT,
  outdated: DesktopSubscriptionReason.OUTDATED,
  no_first_frame: DesktopSubscriptionReason.NO_FIRST_FRAME,
  source_failed: DesktopSubscriptionReason.SOURCE_FAILED,
  resource_exhausted: DesktopSubscriptionReason.RESOURCE_EXHAUSTED,
  internal: DesktopSubscriptionReason.INTERNAL,
  unsupported: DesktopSubscriptionReason.UNSUPPORTED,
  invalid_frame: DesktopSubscriptionReason.INVALID_FRAME,
  closed: DesktopSubscriptionReason.CLOSED,
};

function inverse<K extends string, N extends number>(table: Readonly<Record<K, N>>): Map<N, K> {
  return new Map((Object.entries(table) as [K, N][]).map(([name, number]) => [number, name]));
}
const STEP_OF = inverse(STEPS);
const REASON_OF = inverse(REASONS);

function named<T>(table: ReadonlyMap<number, T>, number: number, what: string): T {
  const found = table.get(number);
  if (found === undefined) throw new Error(`unknown subscription ${what}: ${number}`);
  return found;
}

/** Whether a stale or failed reason is one a stale state may carry. */
function staleReason(reason: FailureReason): Extract<Control, {kind: 'stale'}>['reason'] {
  if (reason === 'unsupported' || reason === 'invalid_frame' || reason === 'closed')
    throw new Error(`a stale subscription cannot carry ${reason}`);
  return reason;
}

type Init = MessageInitShape<typeof DesktopSubscriptionUpdateSchema>;

function controlInit(control: Control): Init {
  switch (control.kind) {
    case 'connecting':
      return {
        update: {
          case: 'control',
          value: {
            state: {
              case: 'connecting',
              value: {step: STEPS[control.step], attempt: control.attempt},
            },
          },
        },
      };
    case 'live':
      return {
        update: {
          case: 'control',
          value: {state: {case: 'live', value: {since: timestampFromMs(control.since)}}},
        },
      };
    case 'stale':
      return {
        update: {
          case: 'control',
          value: {
            state: {
              case: 'stale',
              value: {
                reason: REASONS[control.reason],
                attempt: control.attempt,
                valueAt: timestampFromMs(control.valueAt),
              },
            },
          },
        },
      };
    case 'failed':
      return {
        update: {
          case: 'control',
          value: {
            state: {
              case: 'failed',
              value: {reason: REASONS[control.reason], retrying: control.retrying},
            },
          },
        },
      };
  }
}

function updateInit(update: SubscriptionUpdate): Init {
  switch (update.kind) {
    case 'control':
      return controlInit(update.control);
    case 'begin':
      return {update: {case: 'begin', value: {}}};
    case 'entry':
      return {update: {case: 'entry', value: update.entry}};
    case 'removed':
      return {update: {case: 'removed', value: update.key}};
    case 'synced':
      return {update: {case: 'synced', value: {}}};
  }
}

/** What main sends a window for one update, as protobuf bytes. */
export function encodeUpdate(update: SubscriptionUpdate): Uint8Array {
  const bytes = toBinary(
    DesktopSubscriptionUpdateSchema,
    create(DesktopSubscriptionUpdateSchema, updateInit(update)),
  );
  validatedExactFromBinary(DesktopSubscriptionUpdateSchema, bytes);
  return bytes;
}

function controlOf(state: DesktopSubscriptionControl['state']): Control {
  switch (state.case) {
    case 'connecting':
      return {
        kind: 'connecting',
        step: named(STEP_OF, state.value.step, 'step'),
        attempt: state.value.attempt,
      };
    case 'live':
      return {kind: 'live', since: timestampMs(state.value.since ?? create(TimestampSchema))};
    case 'stale':
      return {
        kind: 'stale',
        reason: staleReason(named(REASON_OF, state.value.reason, 'reason')),
        attempt: state.value.attempt,
        valueAt: timestampMs(state.value.valueAt ?? create(TimestampSchema)),
      };
    case 'failed':
      return {
        kind: 'failed',
        reason: named(REASON_OF, state.value.reason, 'reason'),
        retrying: state.value.retrying,
      };
    default:
      throw new Error('a subscription control holds no state');
  }
}

/** The update in `bytes`, validated, or a throw naming what is wrong. */
export function decodeUpdate(bytes: Uint8Array): SubscriptionUpdate {
  const {update} = validatedExactFromBinary(DesktopSubscriptionUpdateSchema, bytes);
  switch (update.case) {
    case 'control':
      return {kind: 'control', control: controlOf(update.value.state)};
    case 'begin':
      return {kind: 'begin'};
    case 'entry':
      return {kind: 'entry', entry: update.value};
    case 'removed':
      return {kind: 'removed', key: update.value};
    case 'synced':
      return {kind: 'synced'};
    default:
      throw new Error('a subscription update holds no case');
  }
}

/**
 * What a window can ask of the port, one request per port: observe a topic of a host (and
 * optionally one key of it), or ask the host's waiting subscriptions to try again now.
 */
export type SubscriptionCall =
  | {
      readonly kind: 'observe';
      readonly host: HostKey;
      readonly request: SubscribeRequest;
      readonly key: string | undefined;
    }
  | {readonly kind: 'retry'; readonly host: HostKey};

export function encodeSubscriptionCall(call: SubscriptionCall): Uint8Array {
  hostFromKey(call.host);
  const bytes = toBinary(
    DesktopSubscriptionRequestSchema,
    create(DesktopSubscriptionRequestSchema, {
      request:
        call.kind === 'retry'
          ? {case: 'retry', value: create(DesktopRetryRequestSchema, {host: call.host})}
          : {
              case: 'observe',
              value: create(DesktopSubscribeRequestSchema, {
                host: call.host,
                request: call.request,
                ...(call.key === undefined ? {} : {key: call.key}),
              }),
            },
    }),
  );
  validatedExactFromBinary(DesktopSubscriptionRequestSchema, bytes);
  return bytes;
}

/** Main's side: a renderer's request, validated and with no field this build does not know. */
export function parseSubscriptionCall(value: unknown): SubscriptionCall {
  if (!(value instanceof Uint8Array))
    throw new Error('subscription request must be protobuf bytes');
  const {request} = validatedExactFromBinary(DesktopSubscriptionRequestSchema, value);
  switch (request.case) {
    case 'retry':
      return {kind: 'retry', host: request.value.host};
    case 'observe':
      if (request.value.request === undefined)
        throw new Error('subscription request names no topic');
      return {
        kind: 'observe',
        host: request.value.host,
        request: request.value.request,
        key: request.value.key,
      };
    default:
      throw new Error('subscription request holds no case');
  }
}

/**
 * The window's side of the port: one request per observation, updates decoded as they arrive, and
 * the port retired exactly once on every exit.
 */
export function subscriptionBridge(ports: GatewayPortBridge): SubscriptionPort {
  return {
    observe(host, request, key, receive, ended) {
      let done = false;
      let port: {cancel(): void} | undefined;
      const finish = (error?: string): void => {
        if (done) return;
        done = true;
        port?.cancel();
        ended(error);
      };
      const deliver = (bytes: Uint8Array): void => {
        if (done) return;
        try {
          const reply = decodeGatewayReply(bytes);
          if (reply.case === 'bytes') receive(decodeUpdate(reply.value));
          else if (reply.case === 'end') finish(reply.value.error);
        } catch (error) {
          finish(String(error));
        }
      };
      try {
        port = ports.open(
          encodeSubscriptionCall({kind: 'observe', host, request, key}),
          deliver,
          finish,
        );
        if (done) port.cancel();
      } catch (error) {
        finish(String(error));
      }
      return {
        cancel: () => {
          if (done) return;
          done = true;
          port?.cancel();
        },
      };
    },
    retry(host) {
      // Main's one reply is its end, which closes the port; a request main cannot be given leaves
      // the retry to main's own schedule, so a failure here has nothing to report.
      let port: {cancel(): void} | undefined;
      let done = false;
      const finish = (): void => {
        done = true;
        port?.cancel();
      };
      try {
        port = ports.open(encodeSubscriptionCall({kind: 'retry', host}), finish, finish);
        if (done) port.cancel();
      } catch {
        finish();
      }
    },
  };
}
