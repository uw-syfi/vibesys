/**
 * Launching a run (start, resume, reopen): what a gateway state means for the page waiting on it,
 * how a refusal reads, and the file locations in a failed run server's stderr.
 */
import {type Gateway, HomeError, type RunRow} from './home-api.js';

export interface LaunchFailure {
  message: string;
  tail: readonly string[];
  /** Absolute path of the run server's retained stderr. */
  log: string | null;
}

export type LaunchPhase =
  | {kind: 'waiting'}
  | {kind: 'ready'; websocketUrl: string}
  | {kind: 'failed'; failure: LaunchFailure};

const WAITING: LaunchPhase = {kind: 'waiting'};
const failedWith = (message: string): LaunchPhase => ({
  kind: 'failed',
  failure: {message, tail: [], log: null},
});

/** `ended_serving` is ready too: the run page shows how the run ended. */
export function gatewayPhase(gateway: Gateway, error: string | null): LaunchPhase {
  switch (gateway.state) {
    case 'starting':
      return WAITING;
    case 'live':
    case 'ended_serving':
    case 'reopened':
      return gateway.websocket_url === null
        ? WAITING
        : {kind: 'ready', websocketUrl: gateway.websocket_url};
    case 'failed':
      return {
        kind: 'failed',
        failure: {
          message: error ?? 'The run server exited before the run started.',
          tail: gateway.stderr_tail,
          log: gateway.stderr_log,
        },
      };
    case 'stale':
      return failedWith('The run server stopped answering.');
    case 'external':
      return failedWith('Another launcher started a run in this project first.');
    case 'none':
      return failedWith('The run server exited before the run started.');
  }
}

export function launchPhase(row: RunRow | undefined): LaunchPhase {
  return row === undefined ? WAITING : gatewayPhase(row.gateway, row.error);
}

export type LaunchState =
  | {kind: 'idle'}
  | {kind: 'sending'}
  | {kind: 'starting'; runId: string}
  | {kind: 'failed'; failure: LaunchFailure}
  | {kind: 'rejected'; error: HomeError};

function failureOf(error: unknown): LaunchFailure {
  if (!(error instanceof HomeError)) {
    return {message: error instanceof Error ? error.message : String(error), tail: [], log: null};
  }
  const tail = error.details?.['stderr_tail'];
  const log = error.details?.['stderr_log'];
  return {
    message: error.message,
    tail: Array.isArray(tail)
      ? tail.filter((line): line is string => typeof line === 'string')
      : [],
    log: typeof log === 'string' ? log : null,
  };
}

/** A launch that did not start shows its stderr; any other refusal is a message. */
export function launchError(error: unknown): LaunchState {
  const failed =
    !(error instanceof HomeError) || error.code === 'launch_failed' || error.code === 'network';
  return failed ? {kind: 'failed', failure: failureOf(error)} : {kind: 'rejected', error};
}

export function rejectionText(error: HomeError): string {
  const recorded = error.details?.['recorded'];
  if (error.code === 'budget_decrease' && typeof recorded === 'number') {
    return `The run already has a budget of ${recorded}; resume with at least that.`;
  }
  return error.message;
}

/** The footer's line: what is in flight, or why the last attempt was refused. */
export function launchLine(state: LaunchState): {busy: string | null; error: string | null} {
  switch (state.kind) {
    case 'idle':
      return {busy: null, error: null};
    case 'sending':
      return {busy: 'Launching the run server…', error: null};
    case 'starting':
      return {busy: 'Starting the run…', error: null};
    case 'failed':
      return {busy: null, error: state.failure.message};
    case 'rejected':
      return {busy: null, error: rejectionText(state.error)};
  }
}

export interface FileLocation {
  path: string;
  line: number;
  column: number | null;
  /** The matched text, as printed. */
  text: string;
}

export type TailPart = string | FileLocation;

// Python's `File "<path>", line N`, or `<path>.<ext>:<line>[:<col>]` (Rust, Go, TypeScript, gcc).
const LOCATION =
  /File "([^"]+)", line (\d+)|(\/?(?:[\w.~-]+\/)*[\w.-]+\.[A-Za-z]\w*):(\d+)(?::(\d+))?/g;

export function tailParts(line: string): TailPart[] {
  const parts: TailPart[] = [];
  let last = 0;
  for (const match of line.matchAll(LOCATION)) {
    const index = match.index ?? 0;
    if (index > last) parts.push(line.slice(last, index));
    parts.push({
      path: match[1] ?? match[3] ?? '',
      line: Number(match[2] ?? match[4]),
      column: match[5] === undefined ? null : Number(match[5]),
      text: match[0],
    });
    last = index + match[0].length;
  }
  if (last < line.length) parts.push(line.slice(last));
  return parts;
}

/** `path:line[:column]`, absolute when the project root is known. */
export function locationText(root: string | null, location: FileLocation): string {
  const path =
    location.path.startsWith('/') || root === null ? location.path : `${root}/${location.path}`;
  return `${path}:${location.line}${location.column === null ? '' : `:${location.column}`}`;
}
