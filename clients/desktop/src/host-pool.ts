/**
 * The hosts the main process offers and the one `Host` it keeps per host.
 *
 * It turns a picker's host key into a `Host` only for hosts it offered (this machine and the
 * aliases in `~/.ssh/config`), builds each `SshHost` from validated settings, and lists a host's
 * runs through the registry parser. Electron-free: the main process supplies paths and the system.
 */
import {readFile, writeFile} from 'node:fs/promises';
import type {Host} from './host.js';
import {
  EMPTY_SETTINGS,
  type HostId,
  parseSettings,
  SettingsError,
  type SettingsFile,
  settingsFor,
  sshConfigHosts,
  validCommand,
  withHostSettings,
} from './host-settings.js';
import {parseInstanceList, versionSkewMessage} from './instances.js';
import {LocalHost} from './local-host.js';
import type {HostKey, PickerHost, PickerRun} from './picker-protocol.js';
import {SshHost} from './ssh-host.js';

export interface HostPoolOptions {
  readonly sshConfigPath: string;
  readonly settingsPath: string;
  /** The ControlPath template for SSH masters, e.g. `/tmp/vsd-501/%C`. */
  readonly controlPath: string;
  readonly askpass: string;
  /** How this machine runs Python with VibeSys importable. */
  readonly localPython: readonly string[];
  readonly log: (message: string) => void;
}

const LOCAL_KEY: HostKey = 'local';

export function hostKey(id: HostId): HostKey {
  return id.kind === 'local' ? LOCAL_KEY : `ssh:${id.alias}`;
}

export class HostPool {
  readonly #options: HostPoolOptions;
  readonly #hosts = new Map<HostKey, Host>();
  /** Hosts replaced by a settings change, still serving the windows attached through them. */
  readonly #retired: Host[] = [];

  constructor(options: HostPoolOptions) {
    this.#options = options;
  }

  /** This machine, then every concrete alias in `~/.ssh/config`. */
  async offered(): Promise<PickerHost[]> {
    const settings = await this.#settings();
    const aliases = sshConfigHosts(
      await readFile(this.#options.sshConfigPath, 'utf8').catch(() => ''),
    );
    return [
      {key: LOCAL_KEY, label: 'This Mac', vibesysCommand: null, pythonCommand: null},
      ...aliases.map(alias => ({
        key: hostKey({kind: 'ssh', alias}),
        label: alias,
        vibesysCommand: settingsFor(settings, alias).vibesysCommand,
        pythonCommand: settingsFor(settings, alias).pythonCommand ?? '',
      })),
    ];
  }

  /** The host for `key` when the pool offers it, else null. */
  async resolve(key: unknown): Promise<HostId | null> {
    if (key === LOCAL_KEY) return {kind: 'local'};
    if (typeof key !== 'string' || !key.startsWith('ssh:')) return null;
    const offered = await this.offered();
    return offered.some(host => host.key === key) ? {kind: 'ssh', alias: key.slice(4)} : null;
  }

  /** The one `Host` for `id`, created on first use from the current settings. */
  async host(id: HostId): Promise<Host> {
    const key = hostKey(id);
    const existing = this.#hosts.get(key);
    if (existing !== undefined) return existing;
    let host: Host;
    if (id.kind === 'local') {
      host = new LocalHost({python: this.#options.localPython});
    } else {
      const settings = settingsFor(await this.#settings(), id.alias);
      host = new SshHost({
        alias: id.alias,
        vibesysCommand: settings.vibesysCommand,
        ...(settings.pythonCommand === undefined ? {} : {pythonCommand: settings.pythonCommand}),
        controlPath: this.#options.controlPath,
        askpass: this.#options.askpass,
      });
    }
    this.#hosts.set(key, host);
    return host;
  }

  /** How `id` runs VibeSys, for messages. */
  async command(id: HostId): Promise<string> {
    if (id.kind === 'local') return this.#options.localPython.join(' ');
    return settingsFor(await this.#settings(), id.alias).vibesysCommand;
  }

  /** The detached runs live on `id`. */
  async runs(id: HostId): Promise<PickerRun[]> {
    const host = await this.host(id);
    const listing = parseInstanceList(await host.invoke(['instances', 'list', '--json']));
    const name = id.kind === 'local' ? 'This Mac' : id.alias;
    const command = await this.command(id);
    return listing.records.map(record =>
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
              hostName: name,
              vibesysCommand: command,
              protocolVersion: record.protocolVersion,
              vibesysVersion: record.vibesysVersion,
            }),
          },
    );
  }

  /**
   * Save `alias`'s vibesys command and Python command (empty: derive it). The next use of the host
   * runs the new commands; windows already attached keep their host (and its streams) until the app
   * quits.
   */
  async saveCommands(alias: string, command: unknown, python: unknown): Promise<void> {
    const vibesysCommand = validCommand(command, `the vibesys command for ${alias}`);
    if (python !== undefined && typeof python !== 'string') {
      throw new SettingsError(`the Python command for ${alias} must be a string`);
    }
    const settings =
      python === undefined || python.trim() === ''
        ? {vibesysCommand}
        : {vibesysCommand, pythonCommand: validCommand(python, `the Python command for ${alias}`)};
    const current = await this.#settings();
    const next = withHostSettings(current, alias, settings);
    await writeFile(this.#options.settingsPath, `${JSON.stringify(next, null, 2)}\n`, {
      mode: 0o600,
    });
    const key = hostKey({kind: 'ssh', alias});
    const host = this.#hosts.get(key);
    this.#hosts.delete(key);
    if (host !== undefined) this.#retired.push(host);
  }

  async closeAll(): Promise<void> {
    const hosts = [...this.#hosts.values(), ...this.#retired.splice(0)];
    this.#hosts.clear();
    await Promise.all(hosts.map(host => host.close()));
  }

  async #settings(): Promise<SettingsFile> {
    let text: string;
    try {
      text = await readFile(this.#options.settingsPath, 'utf8');
    } catch {
      return EMPTY_SETTINGS;
    }
    try {
      return parseSettings(JSON.parse(text) as unknown);
    } catch (error) {
      this.#options.log(`ignoring ${this.#options.settingsPath}: ${(error as Error).message}`);
      return EMPTY_SETTINGS;
    }
  }
}
