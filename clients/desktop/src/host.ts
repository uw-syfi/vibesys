/**
 * Where a VibeSys server runs, as the main process sees it: the `Host` role.
 *
 * A window is bound to one server on one host. The main process never assumes that host is this
 * machine: it starts servers, runs commands, and opens byte streams only through this interface, so
 * a local host and a remote one (an SSH host) are interchangeable and the page cannot tell them
 * apart. Every implementation passes the contract suite in `testing/host-contract.ts`.
 *
 * Servers are detached runs (`vibesys --detach`), found again through the host's live registry
 * (`vibesys instances list --json`), so every layer above this interface is the same for both.
 */
import type {Duplex} from 'node:stream';
import type {InstanceRecord} from './instances.js';

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

/** A server started by `Host.startServer`. */
export interface ServerHandle {
  /** The endpoint the server listens on; `dial` it to talk to the server. */
  readonly endpoint: Endpoint;
  /** What the server published about itself in the host's registry. */
  readonly record: InstanceRecord;
  /** Settles once the server has been stopped through this handle or its host. */
  readonly exited: Promise<ServerExit>;
  /** Stop the server and wait for it to end. Idempotent. */
  stop(): Promise<void>;
}

export interface StartOptions {
  /** The server's working directory on the host (the project); the host's default otherwise. */
  readonly cwd?: string;
}

/** Why a host operation failed; the closed set callers branch on. */
export type HostErrorKind =
  /** Nothing accepts connections at the endpoint (yet, or any more): the run is not there. */
  | 'unreachable'
  /** The link to the host itself is down or broke (a dropped SSH connection); retrying may work. */
  | 'link'
  /** The host refused the user's credentials, or asked for some that were not given. */
  | 'auth'
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
   * Make sure the link to the host is up, establishing it (and authenticating) when it is not.
   * Rejects with `link` when the host cannot be reached and `auth` when it refused the user. A host
   * on this machine has no link and always resolves while open.
   */
  ensureLink(): Promise<void>;
  /**
   * Start a detached VibeSys server with `args` (the server's own arguments) and resolve with its
   * registry record once it is up. Rejects with `failed` when the server cannot start, carrying
   * its log tail in the message.
   */
  startServer(args: readonly string[], options?: StartOptions): Promise<ServerHandle>;
  /**
   * Open a new byte stream to `endpoint`. Each call is an independent connection, the way the
   * protocol opens one per role. Rejects with `unreachable` when nothing listens there and `link`
   * when the host cannot be reached. A stream that ends abnormally is destroyed with a `HostError`
   * naming why (`unreachable`: the run went away; `link`: the link to the host broke).
   */
  dial(endpoint: Endpoint): Promise<Duplex>;
  /**
   * Run the `vibesys` command line with `argv` and parse its standard output as one JSON document.
   * Rejects with `failed` on a non-zero exit (the message carries its standard error), with
   * `malformed` when the output is not JSON, and with `link` when the host cannot be reached.
   */
  invoke(argv: readonly string[]): Promise<unknown>;
  /**
   * Destroy every stream this host opened and stop every server it started. Later calls reject
   * with `closed`. Idempotent.
   */
  close(): Promise<void>;
}
