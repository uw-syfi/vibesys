/**
 * The real source: calls over the framed stdio of one long-lived child process, which in
 * production is `ssh <host> vibesys-orchestrator connect`. The process is a port, so a test
 * speaks the daemon's side in memory and nothing here spawns anything.
 *
 * One process carries every call of a host. If it dies, each open call fails as `lost` and the
 * next call starts a new process; the caller's redial policy decides when to try.
 */
import {create, type DescMethod, type JsonValue} from '@bufbuild/protobuf';
import {
  type Command,
  decodeValidatedJson,
  encodeJson,
  type ItemsPage,
  OrchestratorService,
  type Plan,
  type Receipt,
  type WatchAgentResponse,
  type WatchRunsResponse,
} from '@vibesys/orchestrator-proto';
import {type DaemonFrame, encodeFrame, FrameDecoder} from './frames.js';
import {AsyncQueue} from './queue.js';
import {type CancelSignal, type OrchestratorSource, SourceError} from './source.js';

/** A child process as bytes in, bytes out. */
export interface ProcessPort {
  write(bytes: Uint8Array): void;
  /** Called with each chunk the process writes to stdout. */
  onData(listener: (chunk: Uint8Array) => void): void;
  /** Called once when the process is gone, with what it said last when it said anything. */
  onClose(listener: (detail: string) => void): void;
  kill(): void;
}

export type LaunchProcess = () => ProcessPort;

interface OpenCall {
  readonly queue: AsyncQueue<unknown>;
}

class Connection {
  readonly #calls = new Map<number, OpenCall>();
  readonly #decoder = new FrameDecoder();
  #next = 1;
  #open = true;

  constructor(
    readonly port: ProcessPort,
    private readonly onGone: () => void,
  ) {
    port.onData(chunk => this.#receive(chunk));
    port.onClose(detail => this.#gone(new SourceError('lost', `the connection closed: ${detail}`)));
  }

  get isOpen(): boolean {
    return this.#open;
  }

  call(method: string, request: unknown, signal: CancelSignal): AsyncIterable<unknown> {
    const queue = new AsyncQueue<unknown>();
    if (!this.#open || signal.aborted) {
      if (this.#open) queue.close();
      else queue.fail(new SourceError('lost', 'the connection is closed'));
      return queue;
    }
    const id = this.#next++;
    this.#calls.set(id, {queue});
    this.port.write(encodeFrame({id, method, request}));
    signal.addEventListener(
      'abort',
      () => {
        if (!this.#calls.delete(id)) return;
        if (this.#open) this.port.write(encodeFrame({id, cancel: true}));
        queue.close();
      },
      {once: true},
    );
    return queue;
  }

  close(): void {
    this.#gone(new SourceError('lost', 'the connection was closed'));
    this.port.kill();
  }

  #receive(chunk: Uint8Array): void {
    let frames: unknown[];
    try {
      frames = this.#decoder.push(chunk);
    } catch (error) {
      this.#gone(new SourceError('invalid', `bad frame: ${String(error)}`));
      this.port.kill();
      return;
    }
    for (const frame of frames) this.#route(frame as DaemonFrame);
  }

  #route(frame: DaemonFrame): void {
    const call = typeof frame?.id === 'number' ? this.#calls.get(frame.id) : undefined;
    if (call === undefined) return;
    if ('response' in frame) call.queue.push(frame.response);
    else if ('end' in frame) {
      this.#calls.delete(frame.id);
      call.queue.close();
    } else if ('error' in frame) {
      this.#calls.delete(frame.id);
      const kind = frame.error.kind === 'refused' ? 'refused' : 'invalid';
      call.queue.fail(new SourceError(kind, frame.error.message));
    }
  }

  #gone(error: SourceError): void {
    if (!this.#open) return;
    this.#open = false;
    for (const call of this.#calls.values()) call.queue.fail(error);
    this.#calls.clear();
    this.onGone();
  }
}

export class StdioSource implements OrchestratorSource {
  #connection: Connection | null = null;

  constructor(private readonly launch: LaunchProcess) {}

  /** Stop the process; open calls fail as `lost`. */
  close(): void {
    this.#connection?.close();
  }

  #call(method: DescMethod, request: unknown, signal: CancelSignal): AsyncIterable<unknown> {
    if (this.#connection === null || !this.#connection.isOpen) {
      const connection: Connection = new Connection(this.launch(), () => {
        if (this.#connection === connection) this.#connection = null;
      });
      this.#connection = connection;
    }
    return this.#connection.call(method.name, request, signal);
  }

  async *#stream<T>(method: DescMethod, request: unknown, signal: CancelSignal): AsyncGenerator<T> {
    for await (const json of this.#call(method, request, signal)) {
      yield decodeValidatedJson(method.output, json as JsonValue) as T;
    }
  }

  async #once<T>(method: DescMethod, request: unknown, signal: CancelSignal): Promise<T> {
    for await (const answer of this.#stream<T>(method, request, signal)) return answer;
    throw new SourceError('lost', `${method.name} ended without an answer`);
  }

  async *watchRuns(afterSequence: bigint, signal: CancelSignal) {
    const method = OrchestratorService.method.watchRuns;
    const request = encodeJson(method.input, create(method.input, {afterSequence}));
    for await (const response of this.#stream<WatchRunsResponse>(method, request, signal)) {
      yield response.body;
    }
  }

  async *watchAgent(
    agentId: string,
    pageSize: number,
    afterSequence: bigint,
    signal: CancelSignal,
  ) {
    const method = OrchestratorService.method.watchAgent;
    const request = encodeJson(
      method.input,
      create(method.input, {agentId, pageSize, afterSequence}),
    );
    yield* this.#stream<WatchAgentResponse>(method, request, signal);
  }

  async listItems(agentId: string, beforeOrdinal: number, pageSize: number, signal: CancelSignal) {
    const method = OrchestratorService.method.listItems;
    const request = encodeJson(
      method.input,
      create(method.input, {agentId, beforeOrdinal, pageSize}),
    );
    const answer = await this.#once<{page?: ItemsPage}>(method, request, signal);
    if (answer.page === undefined)
      throw new SourceError('invalid', 'ListItems answered without a page');
    return answer.page;
  }

  async listPlans(runId: string, signal: CancelSignal): Promise<readonly Plan[]> {
    const method = OrchestratorService.method.listPlans;
    const request = encodeJson(method.input, create(method.input, {runId}));
    return (await this.#once<{plans: Plan[]}>(method, request, signal)).plans;
  }

  async dispatch(command: Command, signal: CancelSignal): Promise<Receipt> {
    const method = OrchestratorService.method.dispatch;
    const request = encodeJson(method.input, create(method.input, {command}));
    const answer = await this.#once<{receipt?: Receipt}>(method, request, signal);
    if (answer.receipt === undefined)
      throw new SourceError('invalid', 'Dispatch answered without a receipt');
    return answer.receipt;
  }
}
