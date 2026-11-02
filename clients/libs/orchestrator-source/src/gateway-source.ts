/**
 * The real `OrchestratorSource`: `OrchestratorService` of a host's `vibesysd`, called through the
 * host gateway connection that main owns (`GatewayBridge` of `@vibesys/ipc`, which brings the ssh
 * link, the provisioned binary, HTTP/2 and the session). This source adds no transport of its own:
 * it encodes a request, decodes and validates each reply, and says in `SourceError` terms why a
 * call did not work. A call is one attempt. When a stream ends, the host machine redials it from
 * the sequence it has reached, on the shared `BackoffSchedule`, which is why the same machine
 * serves this source and the fixture source with no difference.
 */
import {create} from '@bufbuild/protobuf';
import type {HostKey} from '@vibesys/hosts';
import {
  type BuildVersions,
  BuildVersionsSchema,
  type Command,
  Compatibility,
  DispatchRequestSchema,
  DispatchResponseSchema,
  decodeValidated,
  encodeMessage,
  HelloRequestSchema,
  HelloResponseSchema,
  type ItemsPage,
  ListItemsRequestSchema,
  ListItemsResponseSchema,
  ListPlansRequestSchema,
  ListPlansResponseSchema,
  OrchestratorService,
  type Plan,
  type Receipt,
  WatchAgentRequestSchema,
  type WatchAgentResponse,
  WatchAgentResponseSchema,
  WatchRunsRequestSchema,
  type WatchRunsResponse,
  WatchRunsResponseSchema,
} from '@vibesys/orchestrator-proto';
import {type CancelSignal, type OrchestratorSource, SourceError} from './source.js';

/**
 * The part of the gateway bridge this source calls, owned here so the source depends on no
 * transport package: `GatewayBridge` satisfies it. A stream ends with a message and, when the
 * gateway refused the call, the gRPC status; `cancel` ends it with the message `cancelled`.
 */
export interface GatewayCaller {
  unary(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
  ): {readonly result: Promise<Uint8Array>; cancel(): void};
  stream(
    host: HostKey,
    method: string,
    bytes: Uint8Array,
    receive: (bytes: Uint8Array) => void,
    ended: (error?: string, status?: number) => void,
  ): {cancel(): void};
}

/**
 * What this client speaks, in the terms of the daemon's `Hello`. `PROTOCOL_VERSION` is the
 * revision of `OrchestratorService` this client was written against; it moves with the daemon's
 * `ProtocolVersion` (orchestrator/internal/daemon/ports.go) when a change breaks a peer.
 */
export const PROTOCOL_VERSION = 1;
const CLIENT: BuildVersions = create(BuildVersionsSchema, {
  binaryVersion: 'desktop',
  protocolVersion: PROTOCOL_VERSION,
  minProtocolVersion: PROTOCOL_VERSION,
});

/** The most responses a stream holds for a reader that has not taken them; past it the stream is cut. */
export const MAX_QUEUED_FRAMES = 100_000;

/** The gRPC statuses this source tells apart. Any other status is a refusal. */
const UNAVAILABLE = 14;
const UNIMPLEMENTED = 12;
const DATA_LOSS = 15;

const methodPath = (name: string): string => `/${OrchestratorService.typeName}/${name}`;

/**
 * Why a call ended, as a screen words it. UNAVAILABLE is how the daemon says it cannot serve the
 * orchestrator now (another `vibesysd` holds it, or none is configured) and also how the gateway
 * says the connection went away: either way the daemon's own reason is the detail. A call that
 * ends with no status is a transport failure.
 */
export function sourceErrorOf(message: string, status: number | undefined): SourceError {
  const detail = message === '' ? 'the connection ended' : message;
  if (status === undefined || status === UNAVAILABLE) return new SourceError('lost', detail);
  if (status === UNIMPLEMENTED)
    return new SourceError('refused', `vibesysd on this host is too old to run agents (${detail})`);
  if (status === DATA_LOSS) return new SourceError('invalid', detail);
  return new SourceError('refused', detail);
}

/** The status a failed unary call carries, when the gateway refused it (`GatewayError.status`). */
function statusOf(error: unknown): number | undefined {
  const status = (error as {status?: unknown} | null)?.status;
  return typeof status === 'number' ? status : undefined;
}

function messageOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export class GatewaySource implements OrchestratorSource {
  constructor(
    private readonly gateway: GatewayCaller,
    private readonly host: HostKey,
  ) {}

  /** One call, one answer; the call is cancelled with `signal`. */
  async #unary(name: string, request: Uint8Array, signal: CancelSignal): Promise<Uint8Array> {
    if (signal.aborted) throw new SourceError('lost', 'the request was cancelled');
    let call: ReturnType<GatewayCaller['unary']>;
    try {
      call = this.gateway.unary(this.host, methodPath(name), request);
    } catch (error) {
      throw new SourceError('refused', messageOf(error));
    }
    signal.addEventListener('abort', () => call.cancel(), {once: true});
    try {
      return await call.result;
    } catch (error) {
      if (signal.aborted) throw new SourceError('lost', 'the request was cancelled');
      throw sourceErrorOf(messageOf(error), statusOf(error));
    }
  }

  /** Decode a reply; a reply that breaks its schema is an invalid frame, not a half-applied one. */
  #decode<T>(read: () => T): T {
    try {
      return read();
    } catch (error) {
      throw new SourceError('invalid', messageOf(error));
    }
  }

  /**
   * A server stream as an async iterable. It ends quietly when `signal` aborts or the daemon
   * closes it, and throws a `SourceError` when the call fails or a frame breaks its schema. Frames
   * the daemon sent before it failed are delivered first.
   */
  async *#stream<T>(
    name: string,
    request: Uint8Array,
    decode: (bytes: Uint8Array) => T,
    signal: CancelSignal,
  ): AsyncGenerator<T> {
    if (signal.aborted) return;
    const queue: T[] = [];
    // Callbacks of the gateway write this; the loop below reads it.
    const end: {over: boolean; failure: SourceError | null; wake: (() => void) | null} = {
      over: false,
      failure: null,
      wake: null,
    };
    const finish = (failure: SourceError | null): void => {
      if (end.over) return;
      end.over = true;
      end.failure = failure;
      end.wake?.();
    };
    let call: {cancel(): void} | null = null;
    let released = false;
    const release = (): void => {
      if (released) return;
      released = true;
      call?.cancel();
    };
    try {
      call = this.gateway.stream(
        this.host,
        methodPath(name),
        request,
        bytes => {
          if (end.over) return;
          try {
            queue.push(decode(bytes));
          } catch (error) {
            finish(new SourceError('invalid', messageOf(error)));
            release();
            return;
          }
          if (queue.length > MAX_QUEUED_FRAMES) {
            finish(new SourceError('lost', 'the screen fell too far behind the host'));
            release();
            return;
          }
          end.wake?.();
        },
        (error, status) => finish(error === undefined ? null : sourceErrorOf(error, status)),
      );
    } catch (error) {
      throw new SourceError('refused', messageOf(error));
    }
    signal.addEventListener(
      'abort',
      () => {
        finish(null);
        release();
      },
      {once: true},
    );
    try {
      for (;;) {
        const next = queue.shift();
        if (next !== undefined) yield next;
        else if (end.over) break;
        else
          await new Promise<void>(resolve => {
            end.wake = resolve;
          });
      }
    } finally {
      release();
    }
    if (end.failure !== null && !signal.aborted) throw end.failure;
  }

  /** Ask the daemon whether it serves the contract this client speaks. */
  async #hello(signal: CancelSignal): Promise<void> {
    const reply = await this.#unary(
      'Hello',
      encodeMessage(HelloRequestSchema, create(HelloRequestSchema, {client: CLIENT})),
      signal,
    );
    const {daemon, compatibility} = this.#decode(() => decodeValidated(HelloResponseSchema, reply));
    if (compatibility === Compatibility.CLIENT_TOO_OLD)
      throw new SourceError(
        'refused',
        'this app is too old to talk to vibesysd on this host; update the app',
      );
    if (compatibility === Compatibility.DAEMON_TOO_OLD)
      throw new SourceError(
        'refused',
        `vibesysd on this host (${daemon?.binaryVersion ?? 'unknown'}) is too old for this app; update vibesysd there`,
      );
  }

  async *watchRuns(
    afterSequence: bigint,
    signal: CancelSignal,
  ): AsyncGenerator<WatchRunsResponse['body']> {
    // The hello comes first so a host with no orchestrator, or one this app cannot speak to, is
    // named before a stream is opened.
    await this.#hello(signal);
    if (signal.aborted) return;
    const request = encodeMessage(
      WatchRunsRequestSchema,
      create(WatchRunsRequestSchema, {afterSequence}),
    );
    for await (const response of this.#stream(
      'WatchRuns',
      request,
      bytes => decodeValidated(WatchRunsResponseSchema, bytes),
      signal,
    ))
      yield response.body;
  }

  watchAgent(
    agentId: string,
    pageSize: number,
    afterSequence: bigint,
    signal: CancelSignal,
  ): AsyncGenerator<WatchAgentResponse> {
    const request = encodeMessage(
      WatchAgentRequestSchema,
      create(WatchAgentRequestSchema, {agentId, pageSize, afterSequence}),
    );
    return this.#stream(
      'WatchAgent',
      request,
      bytes => decodeValidated(WatchAgentResponseSchema, bytes),
      signal,
    );
  }

  async listItems(
    agentId: string,
    beforeOrdinal: number,
    pageSize: number,
    signal: CancelSignal,
  ): Promise<ItemsPage> {
    const reply = await this.#unary(
      'ListItems',
      encodeMessage(
        ListItemsRequestSchema,
        create(ListItemsRequestSchema, {agentId, beforeOrdinal, pageSize}),
      ),
      signal,
    );
    const {page} = this.#decode(() => decodeValidated(ListItemsResponseSchema, reply));
    if (page === undefined) throw new SourceError('invalid', 'the host answered with no page');
    return page;
  }

  async listPlans(runId: string, signal: CancelSignal): Promise<readonly Plan[]> {
    const reply = await this.#unary(
      'ListPlans',
      encodeMessage(ListPlansRequestSchema, create(ListPlansRequestSchema, {runId})),
      signal,
    );
    return this.#decode(() => decodeValidated(ListPlansResponseSchema, reply)).plans;
  }

  async dispatch(command: Command, signal: CancelSignal): Promise<Receipt> {
    const reply = await this.#unary(
      'Dispatch',
      encodeMessage(DispatchRequestSchema, create(DispatchRequestSchema, {command})),
      signal,
    );
    const {receipt} = this.#decode(() => decodeValidated(DispatchResponseSchema, reply));
    if (receipt === undefined)
      throw new SourceError('invalid', 'the host answered with no receipt');
    return receipt;
  }
}
