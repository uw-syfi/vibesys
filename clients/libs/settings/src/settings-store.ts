/**
 * The app's settings files, `hosts.json` (per-host checkouts), `recent.json` (runs this user
 * opened) and `preferences.json` (appearance, notifications, archived runs), and their one writer.
 *
 * Guarantees:
 *
 * - **One writer.** Every update goes through this store, one at a time across the files, so an
 *   update always reads what the one before it wrote and none drops another's change. With the
 *   app's single-instance lock (`apps/desktop-main`), no other process writes these files.
 * - **Atomic replacement with platform durability.** The text goes to a sibling file and is
 *   flushed before it is renamed over the target, so a reader sees the old file or the new one,
 *   never a partial write. POSIX systems also flush the parent directory before success. Node does
 *   not expose that operation on Windows, where a successful update promises atomic visibility but
 *   not crash-durable publication of the renamed directory entry.
 * - **Changes are announced.** `onRecentChanged` and `onPreferencesChanged` listeners hear every
 *   update of the recent runs and the preferences, so no view has a refresh path to forget. A
 *   failing listener is logged without skipping the other listeners or changing the outcome of an
 *   update that already committed.
 * - **Invalid files are ignored by reads and reported** in the same host-settings snapshot; updates
 *   reject them before writing, so the bytes remain available for repair.
 * - **A recent-runs file with invalid entries is read and updated for the rest.** Each unreadable
 *   entry is skipped and reported with its reason in the same snapshot as the runs kept. Updates
 *   write it back unchanged beside the valid runs, so no entry is dropped or rewritten without the
 *   user's repair.
 *
 * Electron-free: the main process supplies the paths.
 */
import {randomUUID} from 'node:crypto';
import {open, readFile, rename, unlink} from 'node:fs/promises';
import {dirname} from 'node:path';
import type {SkippedRecent} from '@vibesys/runs';
import {EMPTY_SETTINGS, parseSettings, type SettingsFile} from './host-settings.js';
import {DEFAULT_PREFERENCES, type Preferences, parsePreferences} from './preferences.js';
import {
  EMPTY_RECENT,
  type InvalidRecentEntry,
  parseRecent,
  type RecentFile,
  recentDocument,
} from './recent.js';

export interface SettingsStoreOptions {
  readonly settingsPath: string;
  readonly recentPath: string;
  readonly preferencesPath: string;
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

interface RecentRead extends ReadResult<RecentFile> {
  readonly skipped: readonly SkippedRecent[];
  /** The unreadable entries, which a write keeps as they are. */
  readonly invalid: readonly InvalidRecentEntry[];
}

/** One coherent observation of `hosts.json`. */
export interface HostSettingsSnapshot {
  readonly settings: SettingsFile;
  /** Why this observation ignored the file; null when it was valid or absent. */
  readonly problem: string | null;
}

/** One coherent observation of `recent.json`. */
export interface RecentSnapshot {
  readonly recent: RecentFile;
  /** The entries this observation left out, with why; empty when none was invalid. */
  readonly skipped: readonly SkippedRecent[];
}

/** Whether a conditional settings update crossed its atomic replace boundary. */
type SettingsUpdateOutcome = 'committed' | 'cancelled';

export class SettingsStore {
  readonly #options: SettingsStoreOptions;
  readonly #filesystem: SettingsFilesystem;
  /** The last update; each update starts after it settles. */
  #queue: Promise<void> = Promise.resolve();
  readonly #hostsListeners = new Set<(settings: SettingsFile) => void>();
  readonly #recentListeners = new Set<(recent: RecentFile) => void>();
  readonly #preferencesListeners = new Set<(preferences: Preferences) => void>();

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
      this.#log(failure.message);
      return {value: EMPTY_SETTINGS, failure};
    }
    try {
      const parsed = parseSettings(JSON.parse(read.text) as unknown);
      return {value: parsed, failure: null};
    } catch (error) {
      const failure = invalidFile(this.#options.settingsPath, error);
      this.#log(`ignoring ${failure.message}`);
      return {value: EMPTY_SETTINGS, failure};
    }
  }

  /** The recent runs, read afresh. */
  async recent(): Promise<RecentFile> {
    return (await this.#readRecent()).value;
  }

  /** The recent runs and the entries left out of them, read afresh. */
  async recentSnapshot(): Promise<RecentSnapshot> {
    const read = await this.#readRecent();
    return {recent: read.value, skipped: read.skipped};
  }

  async #readRecent(): Promise<RecentRead> {
    const read = await readText(this.#filesystem, this.#options.recentPath);
    if (read.kind === 'missing')
      return {value: EMPTY_RECENT, failure: null, skipped: [], invalid: []};
    if (read.kind === 'failed') {
      const failure = readFailure(this.#options.recentPath, read.error);
      this.#log(failure.message);
      return {value: EMPTY_RECENT, failure, skipped: [], invalid: []};
    }
    try {
      const {file, skipped, invalid} = parseRecent(JSON.parse(read.text) as unknown);
      if (skipped.length > 0) {
        const reasons = skipped.map(entry => entry.reason).join('; ');
        this.#log(`skipping ${skipped.length} entries of ${this.#options.recentPath}: ${reasons}`);
      }
      return {value: file, failure: null, skipped, invalid};
    } catch (error) {
      const failure = invalidFile(this.#options.recentPath, error);
      this.#log(`ignoring ${failure.message}`);
      return {value: EMPTY_RECENT, failure, skipped: [], invalid: []};
    }
  }

  /** The preferences, read afresh; the defaults when the file is absent or invalid. */
  async preferences(): Promise<Preferences> {
    return (await this.#readPreferences()).value;
  }

  async #readPreferences(): Promise<ReadResult<Preferences>> {
    const path = this.#options.preferencesPath;
    const read = await readText(this.#filesystem, path);
    if (read.kind === 'missing') return {value: DEFAULT_PREFERENCES, failure: null};
    if (read.kind === 'failed') {
      const failure = readFailure(path, read.error);
      this.#log(failure.message);
      return {value: DEFAULT_PREFERENCES, failure};
    }
    try {
      return {value: parsePreferences(JSON.parse(read.text) as unknown), failure: null};
    } catch (error) {
      const failure = invalidFile(path, error);
      this.#log(`ignoring ${failure.message}`);
      return {value: DEFAULT_PREFERENCES, failure};
    }
  }

  /** Replace the preferences with `update` of the current ones, then tell the listeners. */
  updatePreferences(update: (preferences: Preferences) => Preferences): Promise<Preferences> {
    return this.#serialize(async () => {
      const read = await this.#readPreferences();
      if (read.failure !== null) throw read.failure;
      const next = update(read.value);
      await writeJson(this.#filesystem, this.#options.preferencesPath, next);
      this.#notify(this.#preferencesListeners, next, 'preferences');
      return next;
    });
  }

  /** Hear every committed preferences update; failures are logged, results are not awaited. */
  onPreferencesChanged(listener: (preferences: Preferences) => void): () => void {
    this.#preferencesListeners.add(listener);
    return () => this.#preferencesListeners.delete(listener);
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
      const next = update(hosts.value);
      const outcome = await writeJson(this.#filesystem, this.#options.settingsPath, next, current);
      if (outcome === 'committed') this.#notify(this.#hostsListeners, next, 'host-settings');
      return outcome;
    });
  }

  /** Hear each committed host setting, including changes made in another window. */
  onHostsChanged(listener: (settings: SettingsFile) => void): () => void {
    this.#hostsListeners.add(listener);
    return () => this.#hostsListeners.delete(listener);
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
      const document = recentDocument(next, recent.invalid);
      if (
        (await writeJson(this.#filesystem, this.#options.recentPath, document, current)) ===
        'cancelled'
      )
        return;
      this.#notify(this.#recentListeners, next, 'recent-runs');
    });
  }

  /** Hear every update; failures are reported after commit and results are not awaited. */
  onRecentChanged(listener: (recent: RecentFile) => void): () => void {
    this.#recentListeners.add(listener);
    return () => this.#recentListeners.delete(listener);
  }

  /** Deliver a committed value without letting a non-owning observer rewrite its outcome. */
  #notify<T>(listeners: ReadonlySet<(value: T) => void>, value: T, what: string): void {
    for (const listener of [...listeners]) {
      try {
        const notified = listener(value);
        void Promise.resolve(notified).catch(error => {
          this.#log(`could not notify a ${what} listener: ${errorMessage(error)}`);
        });
      } catch (error) {
        this.#log(`could not notify a ${what} listener: ${errorMessage(error)}`);
      }
    }
  }

  /** Diagnostics are fire-and-forget and cannot change a durable read or update outcome. */
  #log(message: string): void {
    try {
      const logged = this.#options.log(message);
      void Promise.resolve(logged).catch(() => {
        // The diagnostic has no later owner; consume its rejected thenable here.
      });
    } catch {
      // Diagnostics cannot replace the settings result or the failure being reported.
    }
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

function errorMessage(error: unknown): string {
  try {
    return error instanceof Error ? String(error.message) : String(error);
  } catch {
    return 'an unprintable failure';
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
