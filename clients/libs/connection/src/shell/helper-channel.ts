/**
 * One channel to the host helper: one helper process (a local child, or an ssh exec channel) and
 * its typed JSON lines (`docs/contributing/desktop-connection.md`, "Channels").
 *
 * `HelperChannel.open` sends `hello` and resolves once the helper answered it; every later request
 * carries an id and a deadline and resolves with its own reply, so requests on one channel run
 * concurrently. A request may carry an abort signal: aborting it drops the request, and when its
 * type has a declared withdrawal (`WITHDRAWALS`) the helper offers, tells the helper to stop it.
 * Every line is parsed strictly against the generated contract
 * (`parseHelperMessage`); a line that is not one ends the channel. A run channel is a channel whose
 * one `run.connect` succeeded: from then on it is a byte stream (`RunStream`) carrying the run
 * protocol, ending with the helper's exit status and its `ChannelEnd` line.
 *
 * Failures are `ConnectionError`s with a `Cause` the connection machine decides on. The channel owns
 * its process: disposing it (or aborting `signal`) ends the process and fails what is in flight.
 */
import {Duplex} from 'node:stream';
import {
  type ChannelEndOutcome,
  HELPER_CLIENT_VERSIONS,
  HELPER_FEATURE_BY_FIELD,
  HELPER_FEATURE_BY_REQUEST,
  type HelloReply,
  type HelperError,
  type HelperFeature,
  type HelperMessage,
  type HelperRequest,
  helperFeatureForRequest,
  helperFeaturesForRequest,
  type InstancesChanged,
  parseChannelEnd,
  parseHelperMessage,
} from '@vibesys/contracts/host-helper';
import {
  type AbortRegistration,
  type AbortSignalLike,
  abortReason,
  registerAbort,
  type ScheduleTimeout,
} from '@vibesys/transport';
import type {Cause, CheckoutProblem} from '../state/host-connection.js';
import {bootstrapProblem} from './bootstrap.js';
import {type ProcessRetirement, retireProcess, type SpawnedProcess} from './process.js';

/** A failure of a channel or of an operation on one, with the cause the machine decides on. */
export class ConnectionError extends Error {
  readonly reason: Cause;
  /** For an `incompatible` bootstrap report: which checkout problem it is, or null. */
  readonly checkout: CheckoutProblem | null;

  constructor(
    reason: Cause,
    message: string,
    options?: ErrorOptions & {readonly checkout?: CheckoutProblem | null},
  ) {
    super(message, options);
    this.name = 'ConnectionError';
    this.reason = reason;
    this.checkout = options?.checkout ?? null;
  }

  /** The same failure (cause and checkout problem) in other words. */
  reworded(message: string): ConnectionError {
    return new ConnectionError(this.reason, message, {cause: this, checkout: this.checkout});
  }
}

type InputCompletion = (error: ConnectionError | null) => void;

/**
 * The acknowledged input side of one helper process. Node may report a rejected write by throwing,
 * by passing an error to its completion, or by emitting `error`; this adapter turns all three into
 * one terminal `ConnectionError` and settles every accepted operation exactly once.
 */
class ProcessInput {
  readonly #stream: SpawnedProcess['stdin'];
  readonly #failed: (error: ConnectionError) => void;
  readonly #pending = new Set<InputCompletion>();
  #failure: ConnectionError | null = null;

  constructor(stream: SpawnedProcess['stdin'], failed: (error: ConnectionError) => void) {
    this.#stream = stream;
    this.#failed = failed;
    stream.on('error', error => this.#fail(error));
  }

  write(chunk: string | Buffer, completion: InputCompletion): void {
    this.#operate(completion, done => this.#stream.write(chunk, done));
  }

  end(completion: InputCompletion): void {
    this.#operate(completion, done => this.#stream.end(done));
  }

  destroy(): void {
    this.#stream.destroy();
  }

  #operate(
    completion: InputCompletion,
    start: (done: (error?: Error | null) => void) => void,
  ): void {
    if (this.#failure !== null) {
      completion(this.#failure);
      return;
    }
    if (this.#stream.destroyed || this.#stream.writableEnded) {
      completion(this.#fail(new Error('the process input is already closed')));
      return;
    }
    let settled = false;
    const settle: InputCompletion = error => {
      if (settled) return;
      settled = true;
      this.#pending.delete(settle);
      completion(error);
    };
    this.#pending.add(settle);
    try {
      start(error => {
        if (error != null) this.#fail(error);
        else settle(null);
      });
    } catch (error) {
      this.#fail(error);
    }
  }

  #fail(cause: unknown): ConnectionError {
    if (this.#failure !== null) return this.#failure;
    const detail = cause instanceof Error ? cause.message : String(cause);
    const failure = new ConnectionError('link', `the host helper input failed: ${detail}`, {cause});
    this.#failure = failure;
    for (const settle of [...this.#pending]) settle(failure);
    this.#failed(failure);
    return failure;
  }
}

/** A request the helper answered with an error (`HelperError`), named by its code and path. */
export class HelperRequestError extends Error {
  readonly code: HelperError['code'];
  readonly path: string | null;

  constructor(error: HelperError) {
    super(error.message);
    this.name = 'HelperRequestError';
    this.code = error.code;
    this.path = error.path ?? null;
  }
}

/** How a channel's process ending is explained: the link's own words (ssh) for a status. */
type ExitExplanation = (code: number | null, stderr: string) => ConnectionError;

export interface ChannelOptions {
  /** `CHECKOUT on HOST`, for what the bootstrap reports. */
  readonly where: string;
  readonly appVersion: string;
  readonly deadlineMs: number;
  readonly scheduleTimeout: ScheduleTimeout;
  /** Ends the channel when it aborts, already aborted included: the channel's lifetime. */
  readonly signal: AbortSignal;
  /** Why the process ended, when it ended other than by the helper's own report. */
  readonly explain: ExitExplanation;
  /** Hear each line the bootstrap printed before the helper answered `hello`. */
  readonly heard: (line: string) => void;
  /** A bounded retirement could not prove that this channel's exact process ended. */
  readonly retirementFailed?: (error: ConnectionError) => void;
}

/** The request bodies the app sends; the channel adds `request_id` and `deadline_ms`. */
export type Body = {readonly type: string} & Record<string, unknown>;

/** The reply type of each request type. */
export type ReplyOf<T extends string> = Extract<HelperMessage, {type: T}>;

const FEATURES: readonly HelperFeature[] = [
  ...new Set([
    ...Object.values(HELPER_FEATURE_BY_REQUEST),
    ...Object.values(HELPER_FEATURE_BY_FIELD).flatMap(fields => Object.values(fields ?? {})),
  ]),
];
/**
 * The request types an abort withdraws on the helper's side, each with the request that withdraws
 * it, named by the aborted request's id. Sent only when `hello` negotiated that request's feature;
 * otherwise the aborted request is only dropped here and the helper finishes it on its own.
 */
const WITHDRAWALS: Readonly<Partial<Record<HelperRequest['type'], (requestId: string) => Body>>> = {
  'logs.read': requestId => ({type: 'logs.cancel', read_id: requestId}),
};
/** How long past its own deadline a request may take before this side gives up on it. */
const DEADLINE_SLACK_MS = 2_000;
const STDERR_TAIL = 8192;
/** How many of the bootstrap's lines a channel keeps; it prints one or two. */
const BOOTSTRAP_LINES = 16;
/**
 * The largest line this channel buffers before a newline ends it. A control-channel reply or a
 * bootstrap line is far below this, so a remainder this large means the helper has stopped
 * delimiting lines, not that a real message is this big. Mirrors `NewlineFramer`'s
 * `MAX_REMAINDER_CHARS` (`@vibesys/transport`), the same bound one hop downstream once a run
 * channel is relaying: it exists to bound memory against an endless chunk, not to police message
 * size.
 */
const MAX_BUFFERED_LINE_BYTES = 4 * 1024 * 1024;

interface Pending {
  /** The only successful reply discriminator this request accepts. */
  readonly responseType: string;
  readonly resolve: (message: HelperMessage) => void;
  readonly reject: (error: Error) => void;
  readonly cancel: () => void;
}

export class HelperChannel implements AsyncDisposable {
  readonly #process: SpawnedProcess;
  readonly #input: ProcessInput;
  readonly #options: ChannelOptions;
  readonly #pending = new Map<string, Pending>();
  readonly #pushes = new Set<(push: InstancesChanged) => void>();
  #buffer = Buffer.alloc(0);
  #stderr = '';
  /** The lines the bootstrap printed before `hello` was answered (the last few). */
  readonly #bootstrap: string[] = [];
  #nextId = 1;
  #relay: RunStream | null = null;
  #failure: ConnectionError | null = null;
  /** Recorded separately so an already-started process exit can remain authoritative. */
  #inputFailure: ConnectionError | null = null;
  #retirement: ProcessRetirement | null = null;
  /** What `hello` settled: the versions, the negotiated features, and the checkout. */
  hello: HelloReply | null = null;
  /** Settles once the process has ended, with why (null: the app ended it). */
  readonly ended: Promise<ConnectionError | null>;

  /** True only while neither the protocol nor the process adapter has observed termination. */
  get active(): boolean {
    return this.#failure === null && this.#process.running;
  }

  private constructor(process: SpawnedProcess, options: ChannelOptions) {
    this.#process = process;
    this.#options = options;
    this.#input = new ProcessInput(process.stdin, error => this.#inputFailed(error));
    process.stdout.on('data', (chunk: Buffer) => this.#data(chunk));
    process.stderr.setEncoding('utf8');
    process.stderr.on('data', (chunk: string) => {
      this.#stderr = `${this.#stderr}${chunk}`.slice(-STDERR_TAIL);
    });
    const abort = (): void => this.#close();
    options.signal.addEventListener('abort', abort, {once: true});
    this.ended = process.exit.then(code => {
      options.signal.removeEventListener('abort', abort);
      const failure = this.#failure ?? this.#exitFailure(code) ?? this.#inputFailure;
      this.#rejectAll(failure ?? new ConnectionError('link', 'the helper ended'));
      return failure;
    });
    if (options.signal.aborted) abort();
  }

  /**
   * Start a channel on `process`: `hello`, then the negotiated channel. Aborting `opening` before
   * the helper answered ends the channel too: whoever waited for it no longer does.
   */
  static async open(
    process: SpawnedProcess,
    options: ChannelOptions,
    opening?: AbortSignal,
  ): Promise<HelperChannel> {
    const channel = new HelperChannel(process, options);
    const abandon = (): void => channel.#close();
    opening?.addEventListener('abort', abandon, {once: true});
    if (opening?.aborted === true) abandon();
    try {
      channel.hello = await channel.request({
        type: 'hello',
        client_versions: [...HELPER_CLIENT_VERSIONS],
        features: [...FEATURES],
        app_version: options.appVersion,
      });
      return channel;
    } catch (error) {
      const outcome = await channel.#retire().outcome;
      if (outcome.kind === 'unconfirmed') throw channel.#unconfirmedTerminationError();
      throw await channel.#explained(error);
    } finally {
      opening?.removeEventListener('abort', abandon);
    }
  }

  /**
   * Send `body` and resolve with its reply; rejects with `HelperRequestError` or `ConnectionError`.
   * Aborting `signal` rejects with the abort reason, drops the reply, and withdraws the request
   * when its type has a withdrawal the helper offers (`WITHDRAWALS`).
   */
  request<T extends string>(
    body: Body & {readonly type: T},
    signal?: AbortSignalLike,
  ): Promise<ReplyOf<T>> {
    return this.#request(body, reply => reply, signal);
  }

  /**
   * Register one request and synchronously accept its successful reply inside the line parser.
   * `accept` therefore owns any bytes already buffered after the reply before parsing continues.
   */
  #request<T extends string, Result>(
    body: Body & {readonly type: T},
    accept: (reply: ReplyOf<T>) => Result,
    signal?: AbortSignalLike,
  ): Promise<Result> {
    const terminal = this.#failure ?? this.#inputFailure;
    if (terminal !== null) return Promise.reject(terminal);
    const hello = this.hello;
    const required = helperFeaturesForRequest(body).find(
      feature => hello !== null && !hello.features.includes(feature),
    );
    if (required !== undefined) {
      return Promise.reject(
        new HelperRequestError({
          type: 'error',
          request_id: null,
          code: 'feature_not_negotiated',
          message: `${body.type} requires the ${required} feature`,
          path: null,
        }),
      );
    }
    if (signal?.aborted === true) return Promise.reject(abortReason(signal));
    const requestId = `r${this.#nextId++}`;
    const {deadlineMs, scheduleTimeout} = this.#options;
    return new Promise<Result>((resolve, reject) => {
      let registration: AbortRegistration | null = null;
      const stopTimer = scheduleTimeout(() => {
        this.#pending.delete(requestId);
        registration?.detach();
        reject(new ConnectionError('link', `the helper did not answer ${body.type} in time`));
      }, deadlineMs + DEADLINE_SLACK_MS);
      const cancel = (): void => {
        stopTimer();
        registration?.detach();
      };
      this.#pending.set(requestId, {
        responseType: body.type,
        resolve: message => {
          try {
            resolve(accept(message as ReplyOf<T>));
          } catch (error) {
            reject(error as Error);
          }
        },
        reject,
        cancel,
      });
      if (signal !== undefined) {
        registration = registerAbort(signal, () =>
          this.#withdraw(requestId, body.type, abortReason(signal)),
        );
        if (registration.aborted) {
          this.#pending.delete(requestId);
          stopTimer();
          reject(abortReason(signal));
          return;
        }
      }
      this.#write({...body, request_id: requestId, deadline_ms: deadlineMs});
    });
  }

  /**
   * Drop the aborted request `requestId` (rejecting it with `reason`), then, when its type has a
   * withdrawal the helper offers and the channel still serves, send that withdrawal. The
   * withdrawal's own reply or failure is consumed here: the caller already stopped waiting.
   */
  #withdraw(requestId: string, type: string, reason: Error): void {
    const pending = this.#pending.get(requestId);
    if (pending === undefined) return;
    this.#pending.delete(requestId);
    pending.cancel();
    pending.reject(reason);
    const withdrawal = Object.hasOwn(WITHDRAWALS, type)
      ? WITHDRAWALS[type as HelperRequest['type']]
      : undefined;
    if (withdrawal === undefined || !this.active || this.#inputFailure !== null) return;
    const body = withdrawal(requestId);
    if (!this.supportsRequest(body.type)) return;
    this.#request(body, () => undefined).catch(() => {
      // The withdrawal is best effort: the aborted request's caller is gone, and a failed channel
      // reports itself to whoever still waits on it.
    });
  }

  /** Whether `hello` negotiated the feature needed by this gated request. */
  supportsRequest(type: string): boolean {
    const feature = helperFeatureForRequest(type);
    return feature !== null && this.hello?.features.includes(feature) === true;
  }

  /** Hear every `instances.changed` push; returns the unsubscribe. */
  onPush(listener: (push: InstancesChanged) => void): () => void {
    this.#pushes.add(listener);
    return () => this.#pushes.delete(listener);
  }

  /**
   * Make this channel a relay to run `instanceId` and return its byte stream, with the run id the
   * helper reported. Rejects with `run-gone`, `denied`, or `link`; a failed connect leaves the
   * channel a request channel, which the caller disposes.
   */
  async connectRun(instanceId: string): Promise<{runId: string; stream: RunStream}> {
    try {
      return await this.#request(
        {type: 'run.connect', instance_id: instanceId, role: 'control'},
        reply => {
          // This callback runs synchronously while #line owns the reply. Transfer the complete
          // remainder before #data can interpret another newline as a helper control message.
          const stream = new RunStream({
            process: this.#process,
            input: this.#input,
            first: this.#buffer,
            ended: this.ended,
            stderr: () => this.#stderr,
            retire: () => this.#retire(),
            unconfirmedTerminationError: () => this.#unconfirmedTerminationError(),
          });
          this.#buffer = Buffer.alloc(0);
          this.#relay = stream;
          return {runId: reply.run_id, stream};
        },
      );
    } catch (error) {
      throw connectError(error);
    }
  }

  async [Symbol.asyncDispose](): Promise<void> {
    this.#close();
    const outcome = await this.#retire().outcome;
    if (outcome.kind === 'unconfirmed') throw this.#unconfirmedTerminationError();
    await this.ended;
  }

  #close(): void {
    this.#fail(new ConnectionError('link', 'the channel was closed'));
  }

  #write(message: Record<string, unknown>): void {
    // The input adapter's one terminal callback owns channel failure; this per-write completion is
    // deliberately empty so the same fault cannot take a second path into channel retirement.
    this.#input.write(`${JSON.stringify(message)}\n`, () => {});
  }

  #data(chunk: Buffer): void {
    if (this.#failure !== null) return;
    if (this.#relay !== null) {
      this.#relay.deliver(chunk);
      return;
    }
    this.#buffer = Buffer.concat([this.#buffer, chunk]);
    let newline = this.#buffer.indexOf(0x0a);
    while (newline >= 0 && this.#relay === null && this.#failure === null) {
      const line = this.#buffer.subarray(0, newline).toString('utf8');
      this.#buffer = this.#buffer.subarray(newline + 1);
      this.#line(line);
      newline = this.#buffer.indexOf(0x0a);
    }
    if (this.#buffer.length > MAX_BUFFERED_LINE_BYTES) {
      this.#fail(
        new ConnectionError(
          'incompatible',
          `the host helper wrote more than ${MAX_BUFFERED_LINE_BYTES} bytes without a newline`,
        ),
      );
    }
  }

  #line(line: string): void {
    let message: HelperMessage;
    try {
      message = parseHelperMessage(line);
    } catch (error) {
      // Before `hello` answers, a line is the bootstrap's own report (with its exit status, it says
      // what went wrong; `uv PATH` says what it found); after, an unreadable line breaks the
      // contract.
      if (this.hello === null) {
        this.#bootstrap.push(line);
        this.#bootstrap.splice(0, this.#bootstrap.length - BOOTSTRAP_LINES);
        this.#options.heard(line);
        return;
      }
      this.#fail(
        new ConnectionError(
          'incompatible',
          `the host helper wrote a line this app cannot read: ${(error as Error).message}`,
        ),
      );
      return;
    }
    if (message.type === 'instances.changed') {
      for (const listener of this.#pushes) listener(message);
      return;
    }
    const requestId = message.request_id;
    const pending = requestId === null ? undefined : this.#pending.get(requestId);
    if (pending === undefined) return;
    if (message.type !== 'error' && message.type !== pending.responseType) {
      this.#fail(
        new ConnectionError(
          'incompatible',
          `the host helper answered ${pending.responseType} with ${message.type}`,
        ),
      );
      return;
    }
    this.#pending.delete(requestId as string);
    pending.cancel();
    if (message.type === 'error') pending.reject(new HelperRequestError(message));
    else pending.resolve(message);
  }

  #fail(error: ConnectionError): void {
    if (this.#failure !== null) return;
    this.#failure = error;
    this.#buffer = Buffer.alloc(0);
    this.#rejectAll(error);
    this.#retire();
  }

  #inputFailed(error: ConnectionError): void {
    this.#inputFailure = error;
    if (this.#process.running) {
      this.#fail(error);
      return;
    }
    // Preserve buffered bootstrap/exit evidence when the process had already begun ending: its
    // status is more specific than the secondary write rejection. `ended` rejects work already in
    // flight with that evidence; new requests see this input failure through `#inputFailure`.
    this.#retire();
  }

  #retire(): ProcessRetirement {
    this.#input.destroy();
    if (this.#retirement === null) {
      this.#retirement = retireProcess(this.#process, this.#options.scheduleTimeout);
      void this.#retirement.outcome.then(outcome => {
        if (outcome.kind !== 'unconfirmed') return;
        try {
          this.#options.retirementFailed?.(this.#unconfirmedTerminationError());
        } catch {
          // Retirement ownership must not depend on a diagnostic/lifecycle observer succeeding.
        }
      });
    }
    return this.#retirement;
  }

  #unconfirmedTerminationError(): ConnectionError {
    return new ConnectionError(
      'link',
      `could not confirm termination of the helper for ${this.#options.where}`,
    );
  }

  #rejectAll(error: Error): void {
    for (const pending of this.#pending.values()) {
      pending.cancel();
      pending.reject(error);
    }
    this.#pending.clear();
  }

  /**
   * Why the process ended on its own: the bootstrap's report, while the helper has not answered
   * `hello` yet; otherwise the link's explanation.
   */
  #exitFailure(code: number | null): ConnectionError | null {
    if (code === 0 && this.#relay === null) return null;
    const problem =
      this.hello === null ? bootstrapProblem(code, this.#bootstrap, this.#options.where) : null;
    if (problem !== null) {
      return new ConnectionError('incompatible', problem.message, {checkout: problem.checkout});
    }
    return this.#options.explain(code, this.#stderr);
  }

  /** An error from `open`, with the bootstrap's or the link's account of an early exit. */
  async #explained(error: unknown): Promise<ConnectionError> {
    if (error instanceof HelperRequestError) {
      return new ConnectionError(
        error.code === 'incompatible' ? 'incompatible' : 'link',
        error.message,
      );
    }
    const ended = await this.ended;
    if (ended !== null) return ended;
    return error instanceof ConnectionError ? error : new ConnectionError('link', String(error));
  }
}

function connectError(error: unknown): ConnectionError {
  if (error instanceof ConnectionError) return error;
  if (error instanceof HelperRequestError) {
    if (error.code === 'run_gone') return new ConnectionError('run-gone', error.message);
    if (error.code === 'connect_denied') return new ConnectionError('denied', error.message);
    return new ConnectionError('link', error.message);
  }
  return new ConnectionError('link', String(error));
}

/** What each relay outcome means for the connection; null is an ordinary end. */
const OUTCOME_CAUSES: Readonly<Record<ChannelEndOutcome, Cause | null>> = {
  client_closed: null,
  server_closed: null,
  run_gone: 'run-gone',
  connect_denied: 'denied',
  connect_failed: 'link',
  server_stalled: 'link',
  client_stalled: 'link',
  server_failed: 'link',
  client_failed: 'link',
  server_unresponsive: 'link',
};

/** Why a relayed channel ended, from its `ChannelEnd` line; null for an ordinary end. */
function relayEnd(
  code: number | null,
  stderr: string,
  ended: ConnectionError | null,
): ConnectionError | null {
  if (code === 0) return null;
  const line = stderr.trim().split('\n').at(-1) ?? '';
  try {
    const end = parseChannelEnd(line);
    const cause = OUTCOME_CAUSES[end.outcome];
    return cause === null ? null : new ConnectionError(cause, `${end.outcome}: ${end.detail}`);
  } catch {
    return ended ?? new ConnectionError('link', `the run channel ended with ${code ?? 'a signal'}`);
  }
}

interface RunStreamOptions {
  readonly process: SpawnedProcess;
  readonly input: ProcessInput;
  readonly first: Buffer;
  readonly ended: Promise<ConnectionError | null>;
  readonly stderr: () => string;
  readonly retire: () => ProcessRetirement;
  readonly unconfirmedTerminationError: () => ConnectionError;
}

/**
 * A relayed run channel as a byte stream: stdin is its writable side, stdout its readable side. It
 * ends normally on an ordinary end and is destroyed with a `ConnectionError` naming why otherwise.
 */
export class RunStream extends Duplex {
  readonly #process: SpawnedProcess;
  readonly #input: ProcessInput;
  readonly #retire: () => ProcessRetirement;
  readonly #unconfirmedTerminationError: () => ConnectionError;

  constructor(options: RunStreamOptions) {
    super({allowHalfOpen: false});
    this.#process = options.process;
    this.#input = options.input;
    this.#retire = options.retire;
    this.#unconfirmedTerminationError = options.unconfirmedTerminationError;
    if (options.first.length > 0) this.push(options.first);
    void Promise.all([options.process.exit, options.ended]).then(([code, failure]) => {
      const error = relayEnd(code, options.stderr(), failure);
      if (error === null) this.push(null);
      else this.destroy(error);
    });
  }

  /** Bytes the channel read after it became a relay. */
  deliver(chunk: Buffer): void {
    if (!this.push(chunk)) this.#process.stdout.pause();
  }

  override _read(): void {
    this.#process.stdout.resume();
  }

  override _write(
    chunk: Buffer,
    _encoding: BufferEncoding,
    callback: (error?: Error | null) => void,
  ): void {
    this.#input.write(chunk, error => callback(error));
  }

  override _final(callback: (error?: Error | null) => void): void {
    this.#input.end(error => callback(error));
  }

  override _destroy(error: Error | null, callback: (error: Error | null) => void): void {
    void this.#retire().outcome.then(outcome => {
      callback(
        error ?? (outcome.kind === 'unconfirmed' ? this.#unconfirmedTerminationError() : null),
      );
    });
  }
}
