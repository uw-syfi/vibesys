/**
 * The window's hold on the server Chat reads an ended run through (the `reader`).
 *
 * A run that ended has no server, so its page reads it from its recorded history and Chat has
 * nothing to talk to. While Chat is open on an ended run the page asks the app to attach the
 * run's reader (`attach-reader`: the host starts the run server in its read-only mode for it, and
 * shares it among the clients that ask), folds what that server's session reports into `core`,
 * and lets the run go when Chat is closed or the run is no longer ended (the host's server ends by
 * itself once nothing holds it). A host that cannot serve a run that ended makes the link
 * `unsupported`, which stays: Chat is off for the run, and nothing is asked again.
 *
 * Pure: `reconcileReader` and the replies below are reducers over the reader and the request
 * token counter the page owns, so every sequence of opens, closes and late replies has one
 * outcome, checked as a property in `reader.test.ts`.
 */
import type {CoreState} from '@vibesys/run-state';

type ReaderLink =
  | {readonly kind: 'none'}
  | {readonly kind: 'attaching'; readonly token: number}
  | {readonly kind: 'attached'}
  | {readonly kind: 'failed'; readonly message: string}
  | {readonly kind: 'unsupported'};

export interface ReaderState {
  /** Chat is open on this run. */
  readonly wanted: boolean;
  readonly link: ReaderLink;
  /** The run as the reader's session folds it, chat events included; null until it did. */
  readonly core: CoreState | null;
}

export const READER_IDLE: ReaderState = {wanted: false, link: {kind: 'none'}, core: null};

/** What attaching the reader came to. */
export type ReaderReply =
  | {readonly kind: 'attached'}
  | {readonly kind: 'unsupported'}
  | {readonly kind: 'failed'; readonly message: string};

/** What the screen and the app tell the page about the reader. */
export type ReaderAction =
  /** Chat was opened or closed on the run. */
  | {readonly type: 'chat'; readonly open: boolean}
  /** The app answered the attach asked under `token`. */
  | {readonly type: 'reader-attached'; readonly token: number; readonly reply: ReaderReply}
  /** The reader's session folded the run. */
  | {readonly type: 'reader-core'; readonly core: CoreState};

export type ReaderRequest =
  /** Attach the run's reader to this window, answering `reader-attached` with `token`. */
  | {readonly kind: 'attach-reader'; readonly token: number}
  /** Let the attached reader go (its session closes with it). */
  | {readonly kind: 'release-reader'};

export interface ReaderStep {
  readonly reader: ReaderState;
  readonly requests: readonly ReaderRequest[];
  /** The page's next request token. */
  readonly next: number;
}

function same(reader: ReaderState, next: number): ReaderStep {
  return {reader, requests: [], next};
}

/**
 * The reader the page needs now: attached while Chat is open on an ended run, let go otherwise. A
 * failed attach is forgotten once Chat closes, so opening it again tries again.
 */
export function reconcileReader(reader: ReaderState, ended: boolean, next: number): ReaderStep {
  const {link} = reader;
  if (reader.wanted && ended) {
    if (link.kind !== 'none') return same(reader, next);
    return {
      reader: {...reader, link: {kind: 'attaching', token: next}},
      requests: [{kind: 'attach-reader', token: next}],
      next: next + 1,
    };
  }
  switch (link.kind) {
    case 'attaching':
    case 'attached':
      return {
        reader: {...reader, link: {kind: 'none'}, core: null},
        requests: [{kind: 'release-reader'}],
        next,
      };
    case 'failed':
      return same({...reader, link: {kind: 'none'}}, next);
    case 'none':
    case 'unsupported':
      return same(reader, next);
  }
}

/** Chat was opened or closed on the run. */
export function readerWanted(
  reader: ReaderState,
  wanted: boolean,
  ended: boolean,
  next: number,
): ReaderStep {
  return reconcileReader({...reader, wanted}, ended, next);
}

/**
 * The app answered an attach. A reply for an attach Chat no longer waits for (it was closed while
 * attaching) leaves a binding behind that nothing will use: it is let go, unless a newer attach
 * holds the run's binding, which that attach's own reply settles.
 */
export function readerAttached(
  reader: ReaderState,
  token: number,
  reply: ReaderReply,
  next: number,
): ReaderStep {
  const {link} = reader;
  if (link.kind !== 'attaching' || link.token !== token) {
    const stale = link.kind === 'none' && reply.kind === 'attached';
    return {reader, requests: stale ? [{kind: 'release-reader'}] : [], next};
  }
  switch (reply.kind) {
    case 'attached':
      return same({...reader, link: {kind: 'attached'}}, next);
    case 'unsupported':
      return same({...reader, link: {kind: 'unsupported'}}, next);
    case 'failed':
      return same({...reader, link: {kind: 'failed', message: reply.message}}, next);
  }
}

/** The reader's session folded the run: the chat shows it, while the reader is attached. */
export function readerCore(reader: ReaderState, core: CoreState): ReaderState {
  return reader.link.kind === 'attached' ? {...reader, core} : reader;
}

/** Whether the reader holds, or is getting, a server binding in the app. */
export function readerHeld(reader: ReaderState): boolean {
  return reader.link.kind === 'attaching' || reader.link.kind === 'attached';
}
