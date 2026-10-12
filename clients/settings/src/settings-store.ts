/**
 * The app's settings files, `hosts.json` (per-host checkouts) and `recent.json` (runs this user
 * opened), and their one writer.
 *
 * Guarantees:
 *
 * - **One writer.** Every update goes through this store, one at a time across both files, so an
 *   update always reads what the one before it wrote and none drops another's change. With the
 *   app's single-instance lock (`apps/desktop-main`), no other process writes these files.
 * - **Atomic replace.** The text goes to a sibling file first and is renamed over the target, so a
 *   reader sees the old file or the new one, never a partial write.
 * - **Changes are announced.** `onRecentChanged` listeners hear every update of the recent runs, so
 *   no view has a refresh path to forget.
 * - **Invalid files are ignored by reads and reported** (`hostsProblem` for hosts); updates reject
 *   them before writing, so the bytes remain available for repair.
 *
 * Electron-free: the main process supplies the paths.
 */
import {readFile, rename, unlink, writeFile} from 'node:fs/promises';
import {EMPTY_SETTINGS, parseSettings, type SettingsFile} from './host-settings.js';
import {EMPTY_RECENT, parseRecent, type RecentFile} from './recent.js';

export interface SettingsStoreOptions {
  readonly settingsPath: string;
  readonly recentPath: string;
  readonly log: (message: string) => void;
  readonly filesystem?: SettingsFilesystem;
}

/** The filesystem operations needed to read and atomically replace settings files. */
export interface SettingsFilesystem {
  readText(path: string): Promise<string>;
  writePrivateText(path: string, text: string): Promise<void>;
  remove(path: string): Promise<void>;
  replace(staging: string, target: string): Promise<void>;
}

interface ReadResult<T> {
  readonly value: T;
  /** Why writing from `value` is unsafe; null only when the file was valid or absent. */
  readonly failure: Error | null;
}

/** Whether a conditional settings update crossed its atomic replace boundary. */
type SettingsUpdateOutcome = 'committed' | 'cancelled';

export class SettingsStore {
  readonly #options: SettingsStoreOptions;
  readonly #filesystem: SettingsFilesystem;
  /** The last update; each update starts after it settles. */
  #queue: Promise<void> = Promise.resolve();
  #hostsProblem: string | null = null;
  readonly #recentListeners = new Set<(recent: RecentFile) => void>();

  constructor(options: SettingsStoreOptions) {
    this.#options = options;
    this.#filesystem = options.filesystem ?? NODE_SETTINGS_FILESYSTEM;
  }

  /** Why `hosts.json` was ignored on its last read; null when it is fine or absent. */
  get hostsProblem(): string | null {
    return this.#hostsProblem;
  }

  /** The host settings, read afresh. */
  async hosts(): Promise<SettingsFile> {
    return (await this.#readHosts()).value;
  }

  async #readHosts(): Promise<ReadResult<SettingsFile>> {
    const read = await readText(this.#filesystem, this.#options.settingsPath);
    if (read.kind === 'missing') {
      this.#hostsProblem = null;
      return {value: EMPTY_SETTINGS, failure: null};
    }
    if (read.kind === 'failed') {
      const failure = readFailure(this.#options.settingsPath, read.error);
      this.#hostsProblem = failure.message;
      this.#options.log(failure.message);
      return {value: EMPTY_SETTINGS, failure};
    }
    try {
      const parsed = parseSettings(JSON.parse(read.text) as unknown);
      this.#hostsProblem = null;
      return {value: parsed, failure: null};
    } catch (error) {
      const failure = invalidFile(this.#options.settingsPath, error);
      this.#hostsProblem = failure.message;
      this.#options.log(`ignoring ${this.#hostsProblem}`);
      return {value: EMPTY_SETTINGS, failure};
    }
  }

  /** The recent runs, read afresh. */
  async recent(): Promise<RecentFile> {
    return (await this.#readRecent()).value;
  }

  async #readRecent(): Promise<ReadResult<RecentFile>> {
    const read = await readText(this.#filesystem, this.#options.recentPath);
    if (read.kind === 'missing') return {value: EMPTY_RECENT, failure: null};
    if (read.kind === 'failed') {
      const failure = readFailure(this.#options.recentPath, read.error);
      this.#options.log(failure.message);
      return {value: EMPTY_RECENT, failure};
    }
    try {
      return {value: parseRecent(JSON.parse(read.text) as unknown), failure: null};
    } catch (error) {
      const failure = invalidFile(this.#options.recentPath, error);
      this.#options.log(`ignoring ${failure.message}`);
      return {value: EMPTY_RECENT, failure};
    }
  }

  /**
   * Replace the host settings with `update` of the current ones. `current` is checked at the
   * atomic-replace boundary so a cancelled checkout validation cannot commit staged settings.
   * The outcome makes that boundary explicit: after `committed`, later cancellation cannot revoke
   * the durable write.
   */
  updateHosts(
    update: (current: SettingsFile) => SettingsFile,
    current: () => boolean = () => true,
  ): Promise<SettingsUpdateOutcome> {
    return this.#serialize(async () => {
      const hosts = await this.#readHosts();
      if (hosts.failure !== null) throw hosts.failure;
      return writeJson(this.#filesystem, this.#options.settingsPath, update(hosts.value), current);
    });
  }

  /**
   * Replace the recent runs with `update` of the current ones, then tell the listeners. `current`
   * is checked at the atomic-replace boundary, after staging, so superseded navigation cannot
   * commit or announce an obsolete selection.
   */
  updateRecent(
    update: (recent: RecentFile) => RecentFile,
    current: () => boolean = () => true,
  ): Promise<void> {
    return this.#serialize(async () => {
      const recent = await this.#readRecent();
      if (recent.failure !== null) throw recent.failure;
      const next = update(recent.value);
      if (
        (await writeJson(this.#filesystem, this.#options.recentPath, next, current)) === 'cancelled'
      )
        return;
      for (const listener of this.#recentListeners) listener(next);
    });
  }

  /** Hear every update of the recent runs; returns the unsubscribe. */
  onRecentChanged(listener: (recent: RecentFile) => void): () => void {
    this.#recentListeners.add(listener);
    return () => this.#recentListeners.delete(listener);
  }

  #serialize<T>(write: () => Promise<T>): Promise<T> {
    const next = this.#queue.then(write);
    this.#queue = next.then(
      () => undefined,
      () => undefined,
    );
    return next;
  }
}

type TextRead =
  | {readonly kind: 'text'; readonly text: string}
  | {readonly kind: 'missing'}
  | {readonly kind: 'failed'; readonly error: unknown};

const NODE_SETTINGS_FILESYSTEM: SettingsFilesystem = {
  readText: path => readFile(path, 'utf8'),
  writePrivateText: (path, text) => writeFile(path, text, {mode: 0o600}),
  remove: unlink,
  replace: rename,
};

async function readText(filesystem: SettingsFilesystem, path: string): Promise<TextRead> {
  try {
    return {kind: 'text', text: await filesystem.readText(path)};
  } catch (error) {
    if (typeof error === 'object' && error !== null && 'code' in error && error.code === 'ENOENT') {
      return {kind: 'missing'};
    }
    return {kind: 'failed', error};
  }
}

function readFailure(path: string, cause: unknown): Error {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new Error(`could not read ${path}: ${detail}`, {cause});
}

function invalidFile(path: string, cause: unknown): Error {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new Error(`${path}: ${detail}`, {cause});
}

/** Replace `path` with `value` as JSON, private to the user, atomically (write aside, rename). */
async function writeJson(
  filesystem: SettingsFilesystem,
  path: string,
  value: unknown,
  current: () => boolean = () => true,
): Promise<SettingsUpdateOutcome> {
  const staging = `${path}.${process.pid}.tmp`;
  await filesystem.writePrivateText(staging, `${JSON.stringify(value, null, 2)}\n`);
  if (!current()) {
    await filesystem.remove(staging);
    return 'cancelled';
  }
  try {
    await filesystem.replace(staging, path);
  } catch (error) {
    // Best effort: a failed cleanup must not hide the reason the replace itself failed.
    await filesystem.remove(staging).catch(() => {});
    throw error;
  }
  return 'committed';
}
