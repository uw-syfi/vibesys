/**
 * The hosts the main process offers, the one `Host` it keeps per host, and what it remembers.
 *
 * It turns a welcome-view host key into a `Host` built from that host's validated checkout (This
 * Mac defaults to the checkout the app was built from), checks a checkout before saving it, lists a
 * host's runs through the registry parser and a project's tasks through `vibesys tasks`, and keeps
 * the recent-runs list. Electron-free: the main process supplies paths and the system, and tests
 * supply a `HostFactory` over fakes.
 */
import {readFile} from 'node:fs/promises';
import type {Host} from './host.js';
import {HostError} from './host.js';
import {
  type HostId,
  hostKey,
  hostLabel,
  settingsFor,
  sshConfigHosts,
  validHostPath,
  withHostSettings,
} from './host-settings.js';
import {
  type InstanceRecord,
  parseInstanceList,
  type StopResult,
  versionSkewMessage,
} from './instances.js';
import {checkLocalCheckout, checkoutPython, LocalHost} from './local-host.js';
import {type RecentFile, type RecentRun, remember} from './recent.js';
import type {SettingsStore} from './settings-store.js';
import {SshHost} from './ssh-host.js';
import {parseTaskList, taskListProblem} from './task-list.js';
import type {WelcomeRun} from './welcome-protocol.js';

/** A `Host` that can also check the checkout it was built with. */
export interface CheckedHost extends Host {
  /** Resolve with the checkout's physical path, or reject naming what is missing. */
  verifyCheckout(): Promise<string>;
  /** Live records' `vibesys_root` values, read without a checkout (for a suggestion only). */
  registryRoots(): Promise<string[]>;
}

/** Builds the `Host` for a host and checkout. */
export type HostFactory = (id: HostId, checkout: string) => Promise<CheckedHost>;

export interface HostPoolOptions {
  readonly sshConfigPath: string;
  /** The settings files' one writer. */
  readonly settings: SettingsStore;
  /** This Mac's checkout when the user has not set one: the checkout the app was built from. */
  readonly localCheckout: string;
  readonly factory: HostFactory;
  readonly log: (message: string) => void;
}

/** The real hosts: `SshHost` over the system ssh, `LocalHost` running `uv` here. */
export function systemHostFactory(options: {
  readonly controlPath: string;
  readonly askpass: string;
}): HostFactory {
  return async (id, checkout) => {
    if (id.kind === 'ssh') {
      return new SshHost({alias: id.alias, checkout, ...options});
    }
    const checked = await checkLocalCheckout(checkout);
    const host = new LocalHost({python: checkoutPython(checked)});
    return Object.assign(host, {
      verifyCheckout: async () => (await checkLocalCheckout(checkout)).root,
      registryRoots: async () => [],
    });
  };
}

export class HostPool {
  readonly #options: HostPoolOptions;
  readonly #hosts = new Map<string, Promise<CheckedHost>>();
  /** Hosts replaced by a checkout change, still serving the run attached through them. */
  readonly #retired: Promise<CheckedHost>[] = [];

  constructor(options: HostPoolOptions) {
    this.#options = options;
  }

  /** Why the settings file is ignored, reading it afresh; null when it is fine or absent. */
  async settingsCheck(): Promise<string | null> {
    await this.#options.settings.hosts();
    return this.#options.settings.hostsProblem;
  }

  /** Establish the link to `id` now, prompting when the host asks (the user's own action). */
  async signIn(id: HostId): Promise<void> {
    if (id.kind === 'local') return;
    const host =
      (await this.checkout(id)) === null
        ? await this.#options.factory(id, '/')
        : await this.host(id);
    await host.ensureLink();
  }

  /** The concrete aliases in `~/.ssh/config`. */
  async aliases(): Promise<string[]> {
    return sshConfigHosts(await readFile(this.#options.sshConfigPath, 'utf8').catch(() => ''));
  }

  /** The checkout `id` runs VibeSys from, or null when the user has not set one. */
  async checkout(id: HostId): Promise<string | null> {
    const saved = settingsFor(await this.#options.settings.hosts(), id)?.checkout ?? null;
    if (saved !== null) return saved;
    return id.kind === 'local' ? this.#options.localCheckout : null;
  }

  /** The one `Host` for `id`, built on first use from its checkout. */
  async host(id: HostId): Promise<CheckedHost> {
    const key = hostKey(id);
    const existing = this.#hosts.get(key);
    if (existing !== undefined) return existing;
    const checkout = await this.checkout(id);
    if (checkout === null) {
      throw new HostError('failed', `Set the VibeSys checkout for ${hostLabel(id)} first.`);
    }
    const host = this.#options.factory(id, checkout);
    this.#hosts.set(key, host);
    // A host that could not be built (a missing uv) is built again on the next use.
    host.catch(() => {
      if (this.#hosts.get(key) === host) this.#hosts.delete(key);
    });
    return host;
  }

  /** How `id` runs VibeSys, for messages. */
  async command(id: HostId): Promise<string> {
    return `uv run --project ${(await this.checkout(id)) ?? '<checkout>'} vibesys`;
  }

  /**
   * Check `path` as `id`'s checkout, then save it; resolve with its physical path. The next use of
   * the host runs from it; a run already attached keeps its host until it is replaced.
   */
  async setCheckout(id: HostId, path: unknown): Promise<string> {
    const checkout = validHostPath(path, `the VibeSys checkout on ${hostLabel(id)}`);
    const candidate = this.#options.factory(id, checkout);
    const root = await (await candidate).verifyCheckout();
    await this.#options.settings.updateHosts(current => withHostSettings(current, id, {checkout}));
    const key = hostKey(id);
    const previous = this.#hosts.get(key);
    if (previous !== undefined) this.#retired.push(previous);
    this.#hosts.set(key, candidate);
    return root;
  }

  /**
   * A checkout to suggest for `id`, from the `vibesys_root` its live servers report. Asking also
   * connects, so a host that cannot be reached (`link`) or refuses the user (`auth`) rejects here,
   * before the user is asked for a checkout there; any other failure only means no suggestion.
   */
  async suggestedCheckout(id: HostId): Promise<string | null> {
    if (id.kind === 'local') return null;
    try {
      const probe = await this.#options.factory(id, '/');
      return (await probe.registryRoots())[0] ?? null;
    } catch (error) {
      if (error instanceof HostError && (error.kind === 'link' || error.kind === 'auth'))
        throw error;
      return null;
    }
  }

  /** The live records on `id`. */
  async records(id: HostId): Promise<InstanceRecord[]> {
    const host = await this.host(id);
    return [...parseInstanceList(await host.invoke(['instances', 'list', '--json'])).records];
  }

  /** The detached runs live on `id`, as the welcome view lists them. */
  async runs(id: HostId, records: readonly InstanceRecord[]): Promise<WelcomeRun[]> {
    const command = await this.command(id);
    return records.map(record =>
      record.kind === 'compatible'
        ? {
            id: record.instance.id,
            projectRoot: record.instance.projectRoot,
            runId: record.instance.runId,
            status: record.instance.status,
            startedAt: record.instance.startedAt,
            blocked: null,
          }
        : {
            id: record.id ?? 'unknown',
            projectRoot: '',
            runId: null,
            status: 'incompatible',
            startedAt: 0,
            blocked: versionSkewMessage({
              hostName: hostLabel(id),
              vibesysCommand: command,
              protocolVersion: record.protocolVersion,
              vibesysVersion: record.vibesysVersion,
            }),
          },
    );
  }

  /** Ask `instanceId` on `id` to stop; `force` adds `--force`. */
  async stopRun(id: HostId, instanceId: string, force: boolean): Promise<StopResult> {
    return (await this.host(id)).stopInstance(instanceId, {force});
  }

  /**
   * The tasks `project` defines on `id`, read with `vibesys tasks PROJECT --json`. A failure of the
   * command is reported in plain language (`taskListProblem`); its own output goes to the log.
   */
  async tasks(id: HostId, project: unknown): Promise<string[]> {
    const path = validHostPath(project, 'the project directory');
    const host = await this.host(id);
    let listing: unknown;
    try {
      listing = await host.invoke(['tasks', path, '--json']);
    } catch (error) {
      if (!(error instanceof HostError)) throw error;
      if (error.kind !== 'failed' && error.kind !== 'malformed') throw error;
      this.#options.log(`listing the tasks of ${path} on ${hostLabel(id)}: ${error.message}`);
      const failure =
        error.kind === 'malformed'
          ? ({kind: 'unreadable'} as const)
          : ({kind: 'exited', stderr: error.stderr ?? ''} as const);
      throw new HostError(error.kind, taskListProblem(path, failure));
    }
    try {
      return [...parseTaskList(listing).tasks];
    } catch (error) {
      this.#options.log(`listing the tasks of ${path}: ${(error as Error).message}`);
      throw new HostError('malformed', taskListProblem(path, {kind: 'unreadable'}));
    }
  }

  async recent(): Promise<RecentFile> {
    return this.#options.settings.recent();
  }

  /** Record `run` in the recent runs (through the settings store, the files' one writer). */
  async remember(run: RecentRun): Promise<void> {
    await this.#options.settings.updateRecent(current => remember(current, run));
  }

  async closeAll(): Promise<void> {
    const hosts = [...this.#hosts.values(), ...this.#retired.splice(0)];
    this.#hosts.clear();
    await Promise.all(
      hosts.map(host =>
        host.then(
          built => built.close(),
          () => {},
        ),
      ),
    );
  }
}
