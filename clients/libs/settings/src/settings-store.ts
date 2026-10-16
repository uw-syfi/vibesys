/**
 * The app's settings files, `hosts.json` (per-host checkouts) and `recent.json` (runs this user
 * opened), and their one writer.
 *
 * Guarantees:
 *
 * - **One writer.** Every update goes through this store, one at a time across both files, so an
 *   update always reads what the one before it wrote and none drops another's change. With the
 *   app's single-instance lock (`apps/desktop-main`), no other process writes these files.
 * - **Atomic replacement with platform durability.** The text goes to a sibling file and is
 *   flushed before it is renamed over the target, so a reader sees the old file or the new one,
 *   never a partial write. POSIX systems also flush the parent directory before success. Node does
 *   not expose that operation on Windows, where a successful update promises atomic visibility but
 *   not crash-durable publication of the renamed directory entry.
 * - **Changes are announced.** `onRecentChanged` listeners hear every update of the recent runs, so
 *   no view has a refresh path to forget.
 * - **Invalid files are ignored by reads and reported** in the same host-settings snapshot; updates
 *   reject them before writing, so the bytes remain available for repair.
 *
 * Electron-free: the main process supplies the paths.
 */
import {randomUUID} from 'node:crypto';
import {open, readFile, rename, unlink} from 'node:fs/promises';
import {dirname} from 'node:path';
import {EMPTY_SETTINGS, parseSettings, type SettingsFile} from './host-settings.js';
import {EMPTY_RECENT, parseRecent, type RecentFile} from './recent.js';

export interface SettingsStoreOptions {
  readonly settingsPath: string;
  readonly recentPath: string;
  readonly log: (message: string) => void;
  readonly filesystem?: SettingsFilesystem;
}

/** The filesystem operations needed to read and safely replace settings files. */
export interface SettingsFilesystem {
  readText(path: string): Promise<string>;
  /** Create and file-sync a complete private regular file exclusively beside `target`. */
  stageDurablePrivateText(target: string, text: string): Promise<string>;
  remove(path: string): Promise<void>;
  /**
   * Atomically replace `target`, performing the implementation's available namespace-durability
   * operation before resolving. POSIX implementations sync the parent directory. The Node Windows
   * implementation resolves after rename because Node exposes no directory-sync capability there.
   *
   * A rejection does not promise rollback: rename may have succeeded before the directory sync
   * failed, so the replacement may already be visible even though its crash outcome is unknown.
   */
  replaceAtomically(staging: string, target: string): Promise<void>;
}

/** Whether this Node runtime can flush a renamed entry's parent directory. */
export type DirectorySyncCapability =
  | {readonly kind: 'available'; readonly sync: (directory: string) => Promise<void>}
  | {readonly kind: 'unavailable'};

export interface NodeSettingsFilesystemOptions {
  readonly directorySync?: DirectorySyncCapability;
}

interface ReadResult<T> {
  readonly value: T;
  /** Why writing from `value` is unsafe; null only when the file was valid or absent. */
  readonly failure: Error | null;
}

/** One coherent observation of `hosts.json`. */
export interface HostSettingsSnapshot {
  readonly settings: SettingsFile;
  /** Why this observation ignored the file; null when it was valid or absent. */
  readonly problem: string | null;
}

/** Whether a conditional settings update crossed its atomic replace boundary. */
type SettingsUpdateOutcome = 'committed' | 'cancelled';

export class SettingsStore {
  readonly #options: SettingsStoreOptions;
  readonly #filesystem: SettingsFilesystem;
  /** The last update; each update starts after it settles. */
  #queue: Promise<void> = Promise.resolve();
  readonly #recentListeners = new Set<(recent: RecentFile) => void>();

  constructor(options: SettingsStoreOptions) {
    this.#options = options;
    this.#filesystem = options.filesystem ?? NODE_SETTINGS_FILESYSTEM;
  }

  /** The host settings and this exact read's diagnostic, read afresh. */
  async hosts(): Promise<HostSettingsSnapshot> {
    const read = await this.#readHosts();
    return {settings: read.value, problem: read.failure?.message ?? null};
  }

  async #readHosts(): Promise<ReadResult<SettingsFile>> {
    const read = await readText(this.#filesystem, this.#options.settingsPath);
    if (read.kind === 'missing') {
      return {value: EMPTY_SETTINGS, failure: null};
    }
    if (read.kind === 'failed') {
      const failure = readFailure(this.#options.settingsPath, read.error);
      this.#options.log(failure.message);
      return {value: EMPTY_SETTINGS, failure};
    }
    try {
      const parsed = parseSettings(JSON.parse(read.text) as unknown);
      return {value: parsed, failure: null};
    } catch (error) {
      const failure = invalidFile(this.#options.settingsPath, error);
      this.#options.log(`ignoring ${failure.message}`);
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
   * the installed replacement. Crash durability follows the platform guarantee documented above.
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

/** The Node implementation; its capability is injectable so platform branches are deterministic. */
export class NodeSettingsFilesystem implements SettingsFilesystem {
  readonly #directorySync: DirectorySyncCapability;

  constructor(options: NodeSettingsFilesystemOptions = {}) {
    this.#directorySync = options.directorySync ?? nodeDirectorySync(process.platform);
  }

  readText(path: string): Promise<string> {
    return readFile(path, 'utf8');
  }

  stageDurablePrivateText(target: string, text: string): Promise<string> {
    return stagePrivateText(target, text, 0o600);
  }

  async remove(path: string): Promise<void> {
    await unlink(path);
  }

  async replaceAtomically(staging: string, target: string): Promise<void> {
    await rename(staging, target);
    if (this.#directorySync.kind === 'available') {
      await this.#directorySync.sync(dirname(target));
    }
  }
}

const NODE_SETTINGS_FILESYSTEM: SettingsFilesystem = new NodeSettingsFilesystem();

const MAX_STAGING_ATTEMPTS = 8;

/** Create a complete private regular file without ever opening an existing path. */
async function stagePrivateText(target: string, text: string, mode: number): Promise<string> {
  let collision: unknown;
  for (let attempt = 0; attempt < MAX_STAGING_ATTEMPTS; attempt += 1) {
    const staging = `${target}.${randomUUID()}.tmp`;
    try {
      await writeNewPrivateText(staging, text, mode);
      return staging;
    } catch (error) {
      if (!hasCode(error, 'EEXIST')) throw error;
      collision = error;
    }
  }
  throw new Error(`could not create a private staging file beside ${target}`, {cause: collision});
}

async function writeNewPrivateText(path: string, text: string, mode: number): Promise<void> {
  const file = await open(path, 'wx', mode);
  try {
    await file.writeFile(text);
    // Creation mode is umask-sensitive; chmod the owned descriptor to make the contract exact.
    await file.chmod(mode);
    await file.sync();
    await file.close();
  } catch (error) {
    await file.close().catch(() => {});
    await unlink(path).catch(() => {});
    throw error;
  }
}

function nodeDirectorySync(platform: NodeJS.Platform): DirectorySyncCapability {
  return platform === 'win32' ? {kind: 'unavailable'} : {kind: 'available', sync: syncDirectory};
}

async function syncDirectory(path: string): Promise<void> {
  const directory = await open(path, 'r');
  try {
    await directory.sync();
    await directory.close();
  } catch (error) {
    await directory.close().catch(() => {});
    throw error;
  }
}

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

function hasCode(error: unknown, code: string): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code;
}

function readFailure(path: string, cause: unknown): Error {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new Error(`could not read ${path}: ${detail}`, {cause});
}

function invalidFile(path: string, cause: unknown): Error {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new Error(`${path}: ${detail}`, {cause});
}

/** Replace `path` with private JSON (write aside, file sync, atomic rename, platform durability). */
async function writeJson(
  filesystem: SettingsFilesystem,
  path: string,
  value: unknown,
  current: () => boolean = () => true,
): Promise<SettingsUpdateOutcome> {
  const staging = await filesystem.stageDurablePrivateText(
    path,
    `${JSON.stringify(value, null, 2)}\n`,
  );
  try {
    if (!current()) {
      await filesystem.remove(staging);
      return 'cancelled';
    }
    await filesystem.replaceAtomically(staging, path);
  } catch (error) {
    // Best effort: a failed cleanup must not hide the operation or commit-guard failure.
    await filesystem.remove(staging).catch(() => {});
    throw error;
  }
  return 'committed';
}
