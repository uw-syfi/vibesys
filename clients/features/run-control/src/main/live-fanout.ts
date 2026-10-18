/**
 * Main-process fanout for live run subscriptions. Every page first opens a real run channel, so
 * the relay's open/close contract stays unchanged. A second page's channel is closed before it
 * can replay history; its subscription is answered from the first channel's retained frames and
 * receives that channel's later frames. Control requests keep their independent channel.
 */
import {Duplex} from 'node:stream';
import {NewlineFramer} from '@vibesys/transport';

const DEFAULT_MAX_REPLAY_BYTES = 64 * 1024 * 1024;
const MAX_MEMBER_BUFFER_BYTES = 8 * 1024 * 1024;

type Request = Record<string, unknown>;

interface Group {
  readonly upstream: Duplex;
  readonly framer: NewlineFramer;
  readonly members: Set<FanoutChannel>;
  readonly frames: string[];
  acknowledgement: Request | null;
  bytes: number;
  shareable: boolean;
}

function shareable(request: unknown): request is Request {
  if (request === null || typeof request !== 'object' || Array.isArray(request)) return false;
  const value = request as Request;
  return (
    value['type'] === 'subscribe' &&
    value['after_sequence'] === 0 &&
    (value['tail'] === undefined || value['tail'] === null) &&
    (value['store_id'] === undefined || value['store_id'] === '') &&
    typeof value['request_id'] === 'string'
  );
}

function recordFrame(frame: string): Request | null {
  try {
    const parsed: unknown = JSON.parse(frame);
    if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) return null;
    const message = parsed as Request;
    return typeof message['type'] === 'string' ? message : null;
  } catch {
    return null;
  }
}

/** One server channel opened through the existing host connection. */
export type RunDial = (signal: AbortSignal) => Promise<Duplex>;

export class LiveFanout {
  readonly #groups = new Map<string, Group>();

  constructor(private readonly maxReplayBytes = DEFAULT_MAX_REPLAY_BYTES) {}

  /** Open a run channel, then select sharing only when its first request is a fresh subscription. */
  async open(runId: string, dial: RunDial, signal: AbortSignal): Promise<Duplex> {
    const upstream = await dial(signal);
    if (
      signal.aborted ||
      upstream.destroyed ||
      upstream.closed ||
      upstream.readableEnded ||
      upstream.errored
    ) {
      upstream.destroy();
      throw new Error('run channel closed while opening');
    }
    return new FanoutChannel(upstream, (channel, request) => this.#route(runId, channel, request));
  }

  #route(runId: string, channel: FanoutChannel, request: Request | null): void {
    if (request === null || !shareable(request)) {
      channel.dedicated();
      return;
    }
    let group = this.#groups.get(runId);
    if (group !== undefined && !group.shareable) {
      channel.dedicated();
      return;
    }
    if (group === undefined) {
      group = {
        upstream: channel.upstream,
        framer: new NewlineFramer(),
        members: new Set(),
        frames: [],
        acknowledgement: null,
        bytes: 0,
        shareable: true,
      };
      this.#groups.set(runId, group);
      const owned = group;
      group.upstream.setEncoding('utf8');
      group.upstream.on('data', (chunk: string) => this.#receive(runId, owned, chunk));
      group.upstream.once('end', () => this.#end(runId, owned));
      group.upstream.once('error', () => this.#retire(runId, owned));
      group.upstream.once('close', () => this.#retire(runId, owned));
    }
    group.members.add(channel);
    const joined = group;
    channel.shared(() => {
      joined.members.delete(channel);
      if (joined.members.size === 0) this.#retire(runId, joined);
    }, group.upstream === channel.upstream);
    if (group.acknowledgement !== null) {
      channel.deliver(this.#ack(group.acknowledgement, request));
      for (const frame of group.frames) channel.deliver(frame);
    }
  }

  #receive(runId: string, group: Group, chunk: string): void {
    let frames: string[];
    try {
      frames = group.framer.push(chunk);
    } catch {
      this.#retire(runId, group);
      return;
    }
    for (const frame of frames) {
      if (!this.#deliverFrame(group, frame)) {
        this.#retire(runId, group);
        return;
      }
    }
  }

  #deliverFrame(group: Group, frame: string): boolean {
    const message = recordFrame(frame);
    if (message === null) return false;
    if (message['type'] === 'subscribed') {
      group.acknowledgement = message;
      for (const member of group.members) member.deliver(this.#ack(message, member.request));
      return true;
    }
    if (
      message['type'] === 'event_batch' &&
      typeof message['through_sequence'] === 'number' &&
      group.acknowledgement !== null
    ) {
      group.acknowledgement = {
        ...group.acknowledgement,
        latest_sequence: message['through_sequence'],
      };
    }
    if (message['type'] === 'event_batch' && message['rebootstrap'] === true) {
      group.frames.length = 0;
      group.bytes = 0;
    }
    if (group.shareable) {
      // A new subscriber gets a fresh bootstrap, not the in-loop rebootstrap that the current
      // subscribers saw when the server changed stores.
      const replay =
        message['rebootstrap'] === true ? JSON.stringify({...message, rebootstrap: false}) : frame;
      group.bytes += Buffer.byteLength(replay) + 1;
      if (group.bytes <= this.maxReplayBytes) group.frames.push(replay);
      else {
        group.shareable = false;
        group.frames.length = 0;
      }
    }
    for (const member of group.members) member.deliver(frame);
    return true;
  }

  /** Match the relay's EOF diagnostic, after any complete frames already delivered. */
  #end(runId: string, group: Group): void {
    try {
      group.framer.finish();
    } catch (error) {
      const fault = JSON.stringify({
        type: 'protocol_error',
        code: 'invalid_frame',
        message: error instanceof Error ? error.message : String(error),
      });
      for (const member of group.members) member.deliver(fault);
    }
    this.#retire(runId, group);
  }

  #ack(message: Request, request: Request | null): string {
    return JSON.stringify({
      ...message,
      request_id: request?.['request_id'],
      client_id: request?.['client_id'] ?? '',
    });
  }

  #retire(runId: string, group: Group): void {
    if (this.#groups.get(runId) !== group) return;
    this.#groups.delete(runId);
    group.upstream.destroy();
    for (const member of [...group.members]) member.endFromUpstream();
    group.members.clear();
  }
}

/** A channel whose first request decides whether it relays directly or joins a shared stream. */
class FanoutChannel extends Duplex {
  readonly upstream: Duplex;
  readonly #route: (channel: FanoutChannel, request: Request | null) => void;
  request: Request | null = null;
  #mode: 'undecided' | 'dedicated' | 'shared' = 'undecided';
  #leave: (() => void) | null = null;
  #ownsSharedUpstream = false;
  #firstWrite = true;

  constructor(upstream: Duplex, route: (channel: FanoutChannel, request: Request | null) => void) {
    super({readableHighWaterMark: MAX_MEMBER_BUFFER_BYTES});
    this.upstream = upstream;
    this.#route = route;
    upstream.once('close', () => {
      if (this.#mode === 'undecided') this.destroy();
    });
  }

  override _write(
    chunk: Buffer,
    _encoding: BufferEncoding,
    done: (error?: Error | null) => void,
  ): void {
    const first = this.#firstWrite;
    this.#firstWrite = false;
    if (this.#mode === 'undecided') {
      try {
        this.request = JSON.parse(String(chunk).trim()) as Request;
      } catch {
        this.request = null;
      }
      this.#route(this, this.request);
    }
    if (this.#mode === 'shared') {
      // A server subscription consumes later client input only to detect EOF.
      if (first && this.#ownsSharedUpstream) this.upstream.write(chunk, done);
      else done();
      return;
    }
    this.upstream.write(chunk, done);
  }

  override _read(): void {
    if (this.#mode === 'dedicated') this.upstream.resume();
  }

  override _destroy(error: Error | null, done: (error?: Error | null) => void): void {
    if (this.#mode === 'shared') this.#leave?.();
    else this.upstream.destroy();
    done(error);
  }

  dedicated(): void {
    this.#mode = 'dedicated';
    this.upstream.setEncoding('utf8');
    this.upstream.on('data', (chunk: string) => {
      if (!this.push(chunk)) this.upstream.pause();
    });
    this.upstream.once('end', () => this.push(null));
    this.upstream.once('error', error => this.destroy(error));
    this.upstream.once('close', () => this.push(null));
  }

  shared(leave: () => void, ownsUpstream: boolean): void {
    this.#mode = 'shared';
    this.#leave = leave;
    this.#ownsSharedUpstream = ownsUpstream;
    if (!ownsUpstream) {
      this.upstream.on('error', () => undefined);
      this.upstream.destroy();
    }
  }

  deliver(frame: string): void {
    this.push(`${frame}\n`);
    if (this.readableLength > MAX_MEMBER_BUFFER_BYTES) this.destroy();
  }

  endFromUpstream(): void {
    this.push(null);
  }
}
