/**
 * Launching a run (start, resume, reopen): what a gateway state means for the page waiting on it,
 * how a refusal reads, and the file locations in a failed run server's stderr.
 */
import {useCallback, useEffect, useRef, useState} from 'react';
import {
  type Gateway,
  type HomeClient,
  HomeError,
  type LaunchResult,
  type RunRow,
} from './home-api.js';
import {runHref} from './route.js';

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

const SERVER_EXITED = 'The run server exited before the run started.';

/**
 * `ended_serving` is ready too: the run page shows how the run ended. `recorded` says the run is in
 * the run store, so a failure before it attached is its baseline's, not the run server's start-up.
 */
export function gatewayPhase(
  gateway: Gateway,
  error: string | null,
  recorded = false,
): LaunchPhase {
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
          message: error ?? (recorded ? 'The baseline benchmark failed.' : SERVER_EXITED),
          tail: gateway.stderr_tail,
          log: gateway.stderr_log,
        },
      };
    case 'stale':
      return failedWith('The run server stopped answering.');
    case 'external':
      return failedWith('Another launcher started a run in this project first.');
    case 'none':
      return failedWith(SERVER_EXITED);
  }
}

export function launchPhase(row: RunRow | undefined): LaunchPhase {
  return row === undefined ? WAITING : gatewayPhase(row.gateway, row.error, row.task !== null);
}

export type LaunchState =
  | {kind: 'idle'}
  | {kind: 'sending'}
  | {kind: 'starting'; runId: string}
  | {kind: 'failed'; failure: LaunchFailure}
  | {kind: 'rejected'; error: HomeError};

/** The home server's launch errors are lower-case clauses ("the run server exited with status 1"). */
const sentence = (text: string): string => {
  const capital = text.charAt(0).toUpperCase() + text.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
};

function failureOf(error: unknown): LaunchFailure {
  if (!(error instanceof HomeError)) {
    return {message: error instanceof Error ? error.message : String(error), tail: [], log: null};
  }
  const tail = error.details?.['stderr_tail'];
  const log = error.details?.['stderr_log'];
  return {
    message: sentence(error.message),
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

function rejectionText(error: HomeError): string {
  const recorded = error.details?.['recorded'];
  if (error.code === 'budget_decrease' && typeof recorded === 'number') {
    return `The run already has a budget of ${recorded}; resume with at least that.`;
  }
  if (error.code === 'unknown_run') return 'This run no longer exists.';
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

const POLL_MS = 1_000;
/** Two minutes of polls; a run server that has not attached by then is treated as stuck. */
const MAX_POLLS = 120;
const IDLE: LaunchState = {kind: 'idle'};
const sleep = (ms: number) =>
  new Promise<void>(resolve => {
    setTimeout(resolve, ms);
  });

/** One poll; a home server that did not answer leaves the launch waiting. */
async function poll(client: HomeClient, projectId: string, runId: string): Promise<LaunchPhase> {
  try {
    const {runs} = await client.runs(projectId);
    return launchPhase(runs.find(row => row.run_id === runId));
  } catch (error) {
    if (error instanceof HomeError && error.code === 'network') return WAITING;
    throw error;
  }
}

/**
 * Polls the project's runs until the launched run attaches, fails, the cap runs out, or the page
 * leaves. `pause` waits between polls; tests pass one that resolves at once.
 */
export async function followLaunch(
  client: HomeClient,
  projectId: string,
  result: LaunchResult,
  alive: () => boolean,
  pause: (ms: number) => Promise<void> = sleep,
): Promise<LaunchPhase> {
  let phase = gatewayPhase(result.gateway, null);
  for (let polls = 0; phase.kind === 'waiting' && alive(); polls += 1) {
    if (polls === MAX_POLLS) {
      return failedWith(
        'The run has not attached after two minutes. It may still start; check the home page.',
      );
    }
    await pause(POLL_MS);
    if (!alive()) break;
    phase = await poll(client, projectId, result.run_id);
  }
  return phase;
}

export interface Launch {
  state: LaunchState;
  /** Sends a start, resume or open; once the run attaches, opens its page. */
  start: (projectId: string, send: () => Promise<LaunchResult>) => void;
  reset: () => void;
}

export function useLaunch(client: HomeClient, token: string): Launch {
  const [state, setState] = useState<LaunchState>(IDLE);
  const active = useRef(false);
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);
  const start = useCallback(
    (projectId: string, send: () => Promise<LaunchResult>) => {
      // One launch at a time: a second Start click before the first settles sends nothing.
      if (active.current) return;
      active.current = true;
      setState({kind: 'sending'});
      const settle = (next: LaunchState) => {
        active.current = false;
        if (mounted.current) setState(next);
      };
      send()
        .then(async result => {
          if (mounted.current) setState({kind: 'starting', runId: result.run_id});
          const phase = await followLaunch(client, projectId, result, () => mounted.current);
          if (phase.kind === 'ready')
            window.location.assign(runHref(token, projectId, phase.websocketUrl));
          if (phase.kind === 'failed') settle({kind: 'failed', failure: phase.failure});
        })
        .catch((error: unknown) => settle(launchError(error)));
    },
    [client, token],
  );
  const reset = useCallback(() => setState(IDLE), []);
  return {state, start, reset};
}
