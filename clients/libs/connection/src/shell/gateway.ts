/** One main-owned gateway session; renderer calls keep only bytes and cancellation handles. */
import type {Duplex} from 'node:stream';
import {create, fromBinary, toBinary} from '@bufbuild/protobuf';
import {clockFrom} from '@vibesys/machine';
import {
  API_SCHEMA_HASH,
  HelloRequestSchema,
  type HelloResponse,
  HelloResponseSchema,
  HostCallRequestSchema,
  HostService,
  hostCallBudgets,
  MutateRequestSchema,
  mutateBudgets,
  ReadRequestSchema,
  readBudgets,
  TailRequestSchema,
  TailResponseSchema,
  validatedFromBinary,
  validateHostPayload,
} from '@vibesys/proto';
import {registerAbort, type ScheduleTimeout} from '@vibesys/transport';
import {createActor, fromPromise} from 'xstate';
import {gatewayMachine} from '../state/gateway.js';
import {GatewaySession, GrpcError} from './grpc.js';

export const HELLO_METHOD = `/${HostService.typeName}/${HostService.method.hello.name}`;
export const TAIL_METHOD = `/${HostService.typeName}/${HostService.method.tail.name}`;

export interface GatewayDialer {
  /** Force replaces the artifact after the first schema mismatch. Honor abort and dispose pipes. */
  dial(signal: AbortSignal, force: boolean): Promise<Duplex>;
}

interface LiveCall {
  readonly method: string;
  readonly unary: boolean;
  readonly deadlineMs: number | undefined;
  bytes: Uint8Array;
  readonly receive: (bytes: Uint8Array) => void;
  readonly finish: (error?: unknown) => void;
  abort: AbortController | null;
}

export class GatewayConnection {
  readonly #calls = new Set<LiveCall>();
  readonly #actor;
  #session: GatewaySession | null = null;
  #closed = false;

  constructor(dialer: GatewayDialer, schedule: ScheduleTimeout) {
    this.#actor = createActor(
      gatewayMachine.provide({
        actors: {
          dial: fromPromise(async ({signal}) => {
            try {
              this.#session = await openSession(dialer, schedule, signal, () =>
                this.#actor.send({type: 'lost'}),
              );
              return {ok: true as const};
            } catch (error) {
              return {
                ok: false as const,
                fatal:
                  error instanceof GrpcError &&
                  (error.code === 9 || error.code === 13 || error.code === 15),
                error: String(error),
              };
            }
          }),
        },
        actions: {
          connected: () => {
            for (const call of this.#calls) this.#start(call);
          },
          disconnected: () => {
            for (const call of this.#calls) {
              call.abort?.abort();
              call.abort = null;
            }
            this.#session?.close();
            this.#session = null;
          },
        },
      }),
      {clock: clockFrom(schedule)},
    );
    this.#actor.subscribe(snapshot => {
      if (snapshot.status === 'done') {
        this.#session?.close();
        this.#session = null;
        for (const call of [...this.#calls])
          call.finish(new Error(snapshot.context.error || 'gateway closed'));
      }
    });
    this.#actor.start();
  }

  unary(method: string, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array> {
    let response: Uint8Array | undefined;
    return this.#call(
      method,
      bytes,
      signal,
      message => {
        if (response !== undefined) throw new GrpcError(13, 'unary returned multiple messages');
        response = message;
      },
      true,
    ).then(() => {
      if (response === undefined) throw new GrpcError(13, 'unary returned no message');
      return response;
    });
  }

  stream(
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
  ): Promise<void> {
    return this.#call(method, bytes, signal, receive, false);
  }

  #call(
    method: string,
    bytes: Uint8Array,
    signal: AbortSignal,
    receive: (bytes: Uint8Array) => void,
    unary: boolean,
  ): Promise<void> {
    if (this.#closed || this.#actor.getSnapshot().status === 'done')
      return Promise.reject(new Error('gateway closed'));
    if (signal.aborted) return Promise.reject(signal.reason);
    try {
      validateHostPayload(method, 'input', bytes);
    } catch (error) {
      return Promise.reject(error);
    }
    return new Promise((resolve, reject) => {
      let detach = (): void => {};
      const call: LiveCall = {
        method,
        unary,
        deadlineMs: unary ? unaryDeadline(method, bytes) : undefined,
        bytes,
        receive,
        abort: null,
        finish: error => {
          if (!this.#calls.delete(call)) return;
          detach();
          call.abort?.abort();
          if (error !== undefined) reject(error);
          else resolve();
        },
      };
      this.#calls.add(call);
      const registration = registerAbort(signal, () =>
        call.finish(signal.reason ?? new Error('cancelled')),
      );
      detach = registration.detach;
      if (registration.aborted) call.finish(new Error('cancelled'));
      else if (this.#actor.getSnapshot().matches('ready')) this.#start(call);
    });
  }

  close(): void {
    this.#closed = true;
    this.#actor.send({type: 'close'});
  }

  #start(call: LiveCall): void {
    const session = this.#session;
    if (session === null) return;
    const abort = new AbortController();
    call.abort = abort;
    void session
      .stream(
        call.method,
        call.bytes,
        abort.signal,
        bytes => {
          if (call.abort !== abort) return;
          acceptResponse(call, bytes);
        },
        call.deadlineMs,
      )
      .then(
        () => {
          if (call.abort === abort) call.finish();
        },
        error => {
          if (call.abort !== abort || !this.#calls.has(call)) return;
          if (call.unary || error instanceof GrpcError || call.method !== TAIL_METHOD)
            call.finish(error);
          else this.#actor.send({type: 'lost'});
        },
      );
  }
}

export const HOST_CALL_METHOD = `/${HostService.typeName}/${HostService.method.hostCall.name}`;
export const MUTATE_METHOD = `/${HostService.typeName}/${HostService.method.mutate.name}`;
export const READ_METHOD = `/${HostService.typeName}/${HostService.method.read.name}`;
export const WATCH_METHOD = `/${HostService.typeName}/${HostService.method.watch.name}`;
export const WATCH_RUNS_METHOD = `/${HostService.typeName}/${HostService.method.watchRuns.name}`;

/** Unary RPCs that are not host calls or registered reads. Those budgets come from the registry. */
const UNARY_DEADLINE_MS = 15_000;

function unaryDeadline(method: string, bytes: Uint8Array): number {
  if (method === HOST_CALL_METHOD) {
    const slot = fromBinary(HostCallRequestSchema, bytes).method.case;
    if (slot === undefined) throw new Error('host call has no method');
    return hostCallBudgets[slot].totalMs;
  }
  if (method === READ_METHOD) {
    const slot = fromBinary(ReadRequestSchema, bytes).method.case;
    if (slot === undefined || !(slot in readBudgets)) throw new Error('host read has no method');
    return readBudgets[slot].totalMs;
  }
  if (method === MUTATE_METHOD) {
    const slot = fromBinary(MutateRequestSchema, bytes).method.case;
    if (slot === undefined || !(slot in mutateBudgets))
      throw new Error('host mutate has no method');
    return mutateBudgets[slot].totalMs;
  }
  return UNARY_DEADLINE_MS;
}

async function hello(session: GatewaySession, signal: AbortSignal): Promise<void> {
  const bytes = await session.unary(
    HELLO_METHOD,
    toBinary(HelloRequestSchema, create(HelloRequestSchema, {apiSchemaHash: API_SCHEMA_HASH})),
    signal,
  );
  let response: HelloResponse;
  try {
    response = validatedFromBinary(HelloResponseSchema, bytes);
  } catch (cause) {
    throw new GrpcError(15, `invalid Hello response: ${String(cause)}`);
  }
  if (response.apiSchemaHash !== API_SCHEMA_HASH) {
    throw new GrpcError(
      9,
      `API hash mismatch: client ${API_SCHEMA_HASH}, gateway ${response.apiSchemaHash}`,
    );
  }
}

async function openSession(
  dialer: GatewayDialer,
  schedule: ScheduleTimeout,
  signal: AbortSignal,
  lost: () => void,
): Promise<GatewaySession> {
  for (const force of [false, true]) {
    const duplex = await dialer.dial(signal, force);
    const session = new GatewaySession(duplex, schedule, lost);
    const aborted = registerAbort(signal, () => session.close());
    try {
      if (aborted.aborted) throw new Error('gateway dial cancelled');
      await hello(session, signal);
      if (signal.aborted) throw new Error('gateway dial cancelled');
      return session;
    } catch (error) {
      session.close();
      if (!(error instanceof GrpcError && error.code === 9) || force) throw error;
    } finally {
      aborted.detach();
    }
  }
  throw new Error('gateway handshake did not complete');
}

/** Validate before publishing, then advance only after the consumer accepts the complete batch. */
function acceptResponse(call: LiveCall, bytes: Uint8Array): void {
  let resumed: Uint8Array | undefined;
  try {
    validateHostPayload(call.method, 'output', bytes);
    if (call.method === TAIL_METHOD) {
      const response = fromBinary(TailResponseSchema, bytes);
      const request = fromBinary(TailRequestSchema, call.bytes);
      if (response.throughSeq < request.fromSeq)
        throw new Error('sequence precedes requested cursor');
      resumed = toBinary(TailRequestSchema, {...request, fromSeq: response.throughSeq + 1n});
    }
  } catch (cause) {
    const name = call.method.split('/').at(-1);
    throw new GrpcError(15, `invalid ${name} response: ${String(cause)}`);
  }
  try {
    call.receive(bytes);
  } catch (cause) {
    throw new GrpcError(13, `gateway consumer failed: ${String(cause)}`);
  }
  if (resumed !== undefined) call.bytes = resumed;
}
