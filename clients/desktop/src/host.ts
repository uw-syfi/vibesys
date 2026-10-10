/**
 * Where a VibeSys server runs, as the main process sees it: the `Host` role.
 *
 * A window is bound to one server on one host. The main process never assumes that host is this
 * machine: it starts servers, runs commands, and opens byte streams only through this interface, so
 * a local host and a remote one (an SSH host) are interchangeable and the page cannot tell them
 * apart. Every implementation passes the contract suite in `testing/host-contract.ts`.
 */
import type {Duplex} from 'node:stream';

/** A server's control endpoint, named in the host's own terms. */
export interface Endpoint {
  /** The server's Unix-socket path, as a path on the host (not necessarily on this machine). */
  readonly socketPath: string;
}

/** How a server process the host started has ended. */
export interface ServerExit {
  /** The exit code, or null when the process ended by signal or could not be started. */
  readonly code: number | null;
  /** The last lines of the server's output, for a startup failure report. */
  readonly logTail: string;
}

/** A server process started by `Host.startServer`. */
export interface ServerHandle {
  /** The endpoint the server listens on; `dial` it to talk to the server. */
  readonly endpoint: Endpoint;
  /** Settles once the server process has ended, for whatever reason. */
  readonly exited: Promise<ServerExit>;
  /** Stop the server and wait for it to end. Idempotent. */
  stop(): Promise<void>;
}

/** Why a host operation failed; the closed set callers branch on. */
export type HostErrorKind =
  /** Nothing accepts connections at the endpoint (yet, or any more). */
  | 'unreachable'
  /** A command or server could not run, exited unsuccessfully, or never became ready. */
  | 'failed'
  /** A command's output was not the JSON document it promises. */
  | 'malformed'
  /** The host was closed. */
  | 'closed';

export class HostError extends Error {
  readonly kind: HostErrorKind;

  constructor(kind: HostErrorKind, message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = 'HostError';
    this.kind = kind;
  }
}

export interface Host {
  /**
   * Start a VibeSys server with `args` (the server's own arguments, without `--control-socket`)
   * and resolve once it accepts connections. Rejects with `failed` when the server exits or does
   * not listen in time, carrying its log tail in the message.
   */
  startServer(args: readonly string[]): Promise<ServerHandle>;
  /**
   * Open a new byte stream to `endpoint`. Each call is an independent connection, the way the
   * protocol opens one per role. Rejects with `unreachable` when nothing listens there.
   */
  dial(endpoint: Endpoint): Promise<Duplex>;
  /**
   * Run the `vibesys` command line with `argv` and parse its standard output as one JSON document.
   * Rejects with `failed` on a non-zero exit (the message carries its standard error) and with
   * `malformed` when the output is not JSON.
   */
  invoke(argv: readonly string[]): Promise<unknown>;
  /**
   * Destroy every stream this host opened and stop every server it started. Later calls reject
   * with `closed`. Idempotent.
   */
  close(): Promise<void>;
}
