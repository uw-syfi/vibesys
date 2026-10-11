/**
 * The hosts the welcome view offers: which hosts there are, each host's checkout (checked by
 * starting its helper before it is saved), its tasks and directory completions, and its runs as
 * the helper pushes them (`instances.watch`) to the view. Every host action goes through the
 * host's one connection; nothing here retries or polls.
 */
import {readFile} from 'node:fs/promises';
import type {PathsCompleteReply} from '@vibesys/backend-client/host-helper';
import {
  type HostId,
  type HostKey,
  hostKey,
  hostLabel,
  validHostPath,
} from '../../../common/hosts.js';
import type {HostListing, PathCompletion, WelcomeHost} from '../../../common/welcome-protocol.js';
import {connectionStatus, hostListing} from '../../entities/listing.js';
import {
  type ConnectionManager,
  HelperRequestError,
  type HostConnection,
} from '../../platform/connection/index.js';
import {
  type SettingsStore,
  settingsFor,
  sshConfigHosts,
  withHostSettings,
} from '../../platform/settings/index.js';

export interface HostsDeps {
  readonly manager: ConnectionManager;
  /** The settings files' one writer. */
  readonly settings: SettingsStore;
  /** This Mac's checkout when the user has not set one: the checkout the app was built from. */
  readonly localCheckout: string;
  readonly sshConfigPath: string;
  readonly sshConfigFilesystem?: SshConfigFilesystem;
  /** Send a host's listing to the welcome view. */
  readonly push: (listing: HostListing) => void;
}

/** The filesystem operation needed to read the user's SSH config. */
export interface SshConfigFilesystem {
  readText(path: string): Promise<string>;
}

const NODE_SSH_CONFIG_FILESYSTEM: SshConfigFilesystem = {
  readText: path => readFile(path, 'utf8'),
};

export class Hosts {
  readonly #deps: HostsDeps;
  readonly #sshConfigFilesystem: SshConfigFilesystem;
  /** Hosts whose registry the view hears, by key, with what stops hearing it. */
  readonly #watched = new Map<HostKey, () => void>();

  constructor(deps: HostsDeps) {
    this.#deps = deps;
    this.#sshConfigFilesystem = deps.sshConfigFilesystem ?? NODE_SSH_CONFIG_FILESYSTEM;
    // A new connection for a watched host (its checkout changed) is watched in its place.
    deps.manager.onConnection((key, connection) => {
      if (this.#watched.has(key)) this.#watch(key, connection);
    });
  }

  /** Why the settings file is ignored, reading it afresh; null when it is fine or absent. */
  async settingsProblem(): Promise<string | null> {
    await this.#deps.settings.hosts();
    return this.#deps.settings.hostsProblem;
  }

  /** The concrete aliases in `~/.ssh/config`. */
  async aliases(): Promise<string[]> {
    let text: string;
    try {
      text = await this.#sshConfigFilesystem.readText(this.#deps.sshConfigPath);
    } catch (error) {
      if (isMissingFile(error)) return [];
      throw error;
    }
    return sshConfigHosts(text);
  }

  /** The checkout `id` runs VibeSys from, or null when the user has not set one. */
  async checkout(id: HostId): Promise<string | null> {
    const saved = settingsFor(await this.#deps.settings.hosts(), id)?.checkout ?? null;
    if (saved !== null) return saved;
    return id.kind === 'local' ? this.#deps.localCheckout : null;
  }

  /** The one connection to `id`, from its checkout; rejects when none is set. */
  async connection(id: HostId): Promise<HostConnection> {
    const checkout = await this.checkout(id);
    if (checkout === null) throw new Error(`Set the VibeSys checkout for ${hostLabel(id)} first.`);
    return this.#deps.manager.connection(id, checkout);
  }

  /** `id` as its panel shows it; from now on its runs are pushed to the view. */
  async host(id: HostId): Promise<WelcomeHost> {
    const checkout = await this.checkout(id);
    if (checkout !== null) await this.watch(id);
    return {key: hostKey(id), label: hostLabel(id), checkout};
  }

  /** Push `id`'s runs to the view as its helper reports them, opening its connection (no prompt). */
  async watch(id: HostId): Promise<void> {
    const key = hostKey(id);
    if (this.#watched.has(key)) return;
    this.#watch(key, await this.connection(id));
  }

  /**
   * Check `path` as `id`'s checkout by starting its helper there, then save it; resolve with the
   * checkout's physical path. The next use of the host runs from it.
   */
  async setCheckout(id: HostId, path: unknown): Promise<string> {
    const checkout = validHostPath(path, `the VibeSys checkout on ${hostLabel(id)}`);
    let root: string | null;
    {
      await using candidate = this.#deps.manager.candidate(id, checkout);
      await candidate.ready();
      root = candidate.checkoutRoot;
    }
    await this.#deps.settings.updateHosts(current => withHostSettings(current, id, {checkout}));
    if (this.#watched.has(hostKey(id))) this.#deps.manager.connection(id, checkout);
    return root ?? checkout;
  }

  /** Sign in to `id` now, prompting when the host asks (the user's own action). */
  async signIn(id: HostId): Promise<void> {
    await this.#deps.manager.signIn(id);
  }

  /** The tasks `project` defines on `id`. */
  async tasks(id: HostId, project: unknown): Promise<string[]> {
    const path = validHostPath(project, 'the project directory');
    const connection = await this.connection(id);
    try {
      const reply = await connection.request({type: 'tasks.list', project: path});
      return reply.tasks.tasks.map(task => task.name);
    } catch (error) {
      throw plain(error, `Could not read the tasks of ${path}`);
    }
  }

  /** The directories in `directory` (a path ending in `/`, or `~/`) on `id`. */
  async complete(id: HostId, directory: string): Promise<PathCompletion> {
    const connection = await this.connection(id);
    let reply: PathsCompleteReply;
    try {
      reply = await connection.request({type: 'paths.complete', prefix: directory});
    } catch (error) {
      throw plain(error, `Could not list ${directory}`);
    }
    return {
      directory: reply.directory,
      entries: reply.entries.map(entry => ({
        name: entry.name,
        path: entry.path,
        checkout: entry.vibesys_checkout,
        project: entry.vibesys_project,
      })),
      truncated: reply.truncated,
    };
  }

  #watch(key: HostKey, connection: HostConnection): void {
    this.#watched.get(key)?.();
    const push = this.#deps.push;
    const stopListing = connection.watchInstances(listing => push(hostListing(key, listing)));
    const stopPhase = connection.onPhase(phase => {
      if (phase.kind !== 'ready') {
        push({kind: 'unavailable', host: key, connection: connectionStatus(phase)});
      }
    });
    this.#watched.set(key, () => {
      stopListing();
      stopPhase();
    });
  }
}

function isMissingFile(error: unknown): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === 'ENOENT';
}

/** A helper's refusal in plain words: what failed, and the helper's own reason. */
function plain(error: unknown, what: string): Error {
  if (error instanceof HelperRequestError) return new Error(`${what}: ${error.message}`);
  return error instanceof Error ? error : new Error(String(error));
}
