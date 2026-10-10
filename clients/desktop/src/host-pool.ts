/**
 * The hosts the main process offers, the one `Host` it keeps per host, and what it remembers.
 *
 * It turns a welcome-view host key into a `Host` built from that host's validated checkout (This
 * Mac defaults to the checkout the app was built from), checks a checkout before saving it, lists a
 * host's runs through the registry parser and a project's tasks through `vibesys tasks`, and keeps
 * the recent-runs list. Electron-free: the main process supplies paths and the system, and tests
 * supply a `HostFactory` over fakes.
 */
import {readFile, writeFile} from 'node:fs/promises';
import type {Host} from './host.js';
import {HostError} from './host.js';
import {
  EMPTY_SETTINGS,
  type HostId,
  hostKey,
  hostLabel,
  parseSettings,
  type SettingsFile,
  settingsFor,
  sshConfigHosts,
  validHostPath,
  withHostSettings,
} from './host-settings.js';
import {type InstanceRecord, parseInstanceList, versionSkewMessage} from './instances.js';
import {checkLocalCheckout, checkoutPython, LocalHost} from './local-host.js';
import {EMPTY_RECENT, parseRecent, type RecentFile, type RecentRun, remember} from './recent.js';
import {SshHost} from './ssh-host.js';
import {parseTaskList} from './task-list.js';
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
  readonly settingsPath: string;
  readonly recentPath: string;
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
  #settingsProblem: string | null = null;

  constructor(options: HostPoolOptions) {
    this.#options = options;
  }

  /** Why the settings file was ignored, when it was. */
  get settingsProblem(): string | null {
    return this.#settingsProblem;
  }

  /** Why the settings file is ignored, reading it afresh; null when it is fine or absent. */
  async settingsCheck(): Promise<string | null> {
    await this.#settings();
    return this.#settingsProblem;
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
    const saved = settingsFor(await this.#settings(), id)?.checkout ?? null;
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
    const next = withHostSettings(await this.#settings(), id, {checkout});
    await writeFile(this.#options.settingsPath, `${JSON.stringify(next, null, 2)}\n`, {
      mode: 0o600,
    });
    const key = hostKey(id);
    const previous = this.#hosts.get(key);
    if (previous !== undefined) this.#retired.push(previous);
    this.#hosts.set(key, candidate);
    return root;
  }

  /** A checkout to suggest for `id`, from the `vibesys_root` its live servers report. */
  async suggestedCheckout(id: HostId): Promise<string | null> {
    if (id.kind === 'local') return null;
    try {
      const probe = await this.#options.factory(id, '/');
      return (await probe.registryRoots())[0] ?? null;
    } catch {
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

  /** The tasks `project` defines on `id`, read with `vibesys tasks PROJECT --json`. */
  async tasks(id: HostId, project: unknown): Promise<string[]> {
    const path = validHostPath(project, 'the project directory');
    const host = await this.host(id);
    return [...parseTaskList(await host.invoke(['tasks', path, '--json'])).tasks];
  }

  async recent(): Promise<RecentFile> {
    let text: string;
    try {
      text = await readFile(this.#options.recentPath, 'utf8');
    } catch {
      return EMPTY_RECENT;
    }
    try {
      return parseRecent(JSON.parse(text) as unknown);
    } catch (error) {
      this.#options.log(`ignoring ${this.#options.recentPath}: ${(error as Error).message}`);
      return EMPTY_RECENT;
    }
  }

  async remember(run: RecentRun): Promise<void> {
    const next = remember(await this.recent(), run);
    await writeFile(this.#options.recentPath, `${JSON.stringify(next, null, 2)}\n`, {mode: 0o600});
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

  async #settings(): Promise<SettingsFile> {
    let text: string;
    try {
      text = await readFile(this.#options.settingsPath, 'utf8');
    } catch {
      this.#settingsProblem = null;
      return EMPTY_SETTINGS;
    }
    try {
      const parsed = parseSettings(JSON.parse(text) as unknown);
      this.#settingsProblem = null;
      return parsed;
    } catch (error) {
      this.#settingsProblem = `${this.#options.settingsPath}: ${(error as Error).message}`;
      this.#options.log(`ignoring ${this.#settingsProblem}`);
      return EMPTY_SETTINGS;
    }
  }
}
