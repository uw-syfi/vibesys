/**
 * The ended run's trace events as the Run screen reads them from the app (`RunPagePorts.readTrace`):
 * a page of the run's recorded events with only the events the trace folds kept, and the pages kept
 * for the session so opening the run again does not read the journal again.
 *
 * The journal's other events (most of its bytes) are dropped before they are validated or held, so
 * a page costs its trace events only. Pages are held by the run's server instance as well as its
 * identity: an ended instance's journal no longer changes, while a run that is resumed is served
 * by a new instance, so a page that reached the old journal's end is never taken for the new one's.
 * At most `RUNS_HELD` runs are held, the least recently read dropped first.
 */
import {validateRunEvent} from '@vibesys/contracts';
import type {RunHistoryPage, WelcomeResult} from '@vibesys/ipc';
import {type HistoryReply, isTraceEventType} from '../../state/index.js';

/** The most runs whose pages are held. */
const RUNS_HELD = 8;

type Page = Extract<HistoryReply, {readonly kind: 'page'}>;

export class TracePageCache {
  /** Held pages by run, then by the request that produced them; least recently read run first. */
  readonly #runs = new Map<string, Map<string, Page>>();

  get(run: string, from: number, max: number): Page | undefined {
    const pages = this.#runs.get(run);
    if (pages === undefined) return undefined;
    this.#runs.delete(run);
    this.#runs.set(run, pages);
    return pages.get(`${from}:${max}`);
  }

  hold(run: string, from: number, max: number, page: Page): void {
    const pages = this.#runs.get(run) ?? new Map<string, Page>();
    pages.set(`${from}:${max}`, page);
    this.#runs.delete(run);
    this.#runs.set(run, pages);
    for (const oldest of [...this.#runs.keys()]) {
      if (this.#runs.size <= RUNS_HELD) break;
      this.#runs.delete(oldest);
    }
  }
}

/** The cache the app's windows share while it runs. */
export const sharedTracePages = new TracePageCache();

function isTraceEvent(event: unknown): boolean {
  return (
    typeof event === 'object' && event !== null && 'type' in event && isTraceEventType(event.type)
  );
}

/** A page with its trace events kept and validated; one that is not valid fails the page. */
function traceReply(page: RunHistoryPage): HistoryReply {
  if (page.kind === 'unsupported') return page;
  try {
    return {
      kind: 'page',
      events: page.events.filter(isTraceEvent).map(validateRunEvent),
      next: page.next,
      complete: page.complete,
    };
  } catch (error) {
    return {
      kind: 'failed',
      message: `the run's history holds an unreadable event: ${String(error)}`,
    };
  }
}

/**
 * The port that reads the trace events of the run that `run()` names (its identity and the server
 * instance that ended it, when the read is made) through `read` (one page of its recorded events),
 * from `cache` when the page was read before.
 */
export function traceReader(
  run: () => string,
  cache: TracePageCache,
  read: (from: number, max: number) => Promise<WelcomeResult<RunHistoryPage>>,
): (from: number, max: number) => Promise<HistoryReply> {
  return async (from, max) => {
    const key = run();
    const held = cache.get(key, from, max);
    if (held !== undefined) return held;
    const result = await read(from, max);
    if (!result.ok) return {kind: 'failed', message: result.error};
    const reply = traceReply(result.value);
    if (reply.kind === 'page') cache.hold(key, from, max, reply);
    return reply;
  };
}
