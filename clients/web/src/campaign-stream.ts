/**
 * The transport seam for the live campaign: a source of parsed frames with one
 * cleanup path. `EventSourceCampaignStream` wraps the browser's `EventSource`
 * and validates every message at this boundary; `FakeCampaignStream` drives the
 * same interface from memory so the hook can be tested without a server, timers,
 * or sleeps.
 */
import {type CampaignFrame, parseCampaignFrame} from './campaign-frames.js';

export type FrameListener = (frame: CampaignFrame) => void;
export type StreamErrorListener = (error: Error) => void;

export interface CampaignStream {
  /** Begin receiving frames. Returns an unsubscribe that releases the transport. */
  subscribe(onFrame: FrameListener, onError: StreamErrorListener): () => void;
}

interface MessageLike {
  readonly data: string;
}

interface EventSourceLike {
  onmessage: ((event: MessageLike) => void) | null;
  onerror: ((event: unknown) => void) | null;
  readonly readyState: number;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

const openEventSource: EventSourceFactory = url =>
  new EventSource(url) as unknown as EventSourceLike;

export class EventSourceCampaignStream implements CampaignStream {
  constructor(
    private readonly url: string,
    private readonly open: EventSourceFactory = openEventSource,
  ) {}

  subscribe(onFrame: FrameListener, onError: StreamErrorListener): () => void {
    const source = this.open(this.url);
    source.onmessage = event => {
      let frame: CampaignFrame;
      try {
        frame = parseCampaignFrame(JSON.parse(event.data));
      } catch (error) {
        onError(error instanceof Error ? error : new Error(String(error)));
        return;
      }
      onFrame(frame);
    };
    source.onerror = () => {
      // The browser retries a dropped connection on its own (readyState goes
      // back to CONNECTING, not CLOSED) and replays the log; the fold ignores
      // already-applied sequences, so a reconnect is a no-op and must not raise
      // the banner. Only a closed connection is a terminal failure worth
      // surfacing.
      if (source.readyState === EventSource.CLOSED)
        onError(new Error('Campaign stream connection error'));
    };
    return () => {
      source.onmessage = null;
      source.onerror = null;
      source.close();
    };
  }
}

/** In-memory stream for tests: frames are pushed synchronously via `emit`. */
export class FakeCampaignStream implements CampaignStream {
  private onFrame: FrameListener | null = null;
  private onError: StreamErrorListener | null = null;
  private subscribed = false;

  subscribe(onFrame: FrameListener, onError: StreamErrorListener): () => void {
    if (this.subscribed) throw new Error('FakeCampaignStream supports a single subscriber');
    this.subscribed = true;
    this.onFrame = onFrame;
    this.onError = onError;
    return () => {
      this.subscribed = false;
      this.onFrame = null;
      this.onError = null;
    };
  }

  get active(): boolean {
    return this.subscribed;
  }

  emit(frame: CampaignFrame): void {
    this.onFrame?.(frame);
  }

  emitAll(frames: Iterable<CampaignFrame>): void {
    for (const frame of frames) this.emit(frame);
  }

  fail(error: Error): void {
    this.onError?.(error);
  }
}
