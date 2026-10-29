/**
 * The app's protobuf settings files, `hosts.pb`, `recent.pb` and `preferences.pb`, and their
 * one writer. Original JSON siblings are strict, read-only import sources, retained unchanged.
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
import {decodeRetainedSettings, EMPTY_SETTINGS, type SettingsFile} from './host-settings.js';
import {DEFAULT_PREFERENCES, type Preferences, parsePreferences} from './preferences.js';
import {
  EMPTY_RECENT,
  type InvalidRecentEntry,
  parseRecent,
  type RecentFile,
  recentDocument,
} from './recent.js';
import {
  decodePreferences,
  decodeRecent,
  decodeSettings,
  encodePreferences,
  encodeRecent,
  encodeSettings,
  settingsBinaryPath,
} from './settings-codec.js';

export interface SettingsStoreOptions {
  /** Retained JSON source paths; protobuf siblings become authoritative after first import. */
  readonly settingsPath: string;
  readonly recentPath: string;
  readonly preferencesPath: string;
  readonly log: (message: string) => void;
  readonly filesystem?: SettingsFilesystem;
}

/** The filesystem operations needed to read and safely replace settings files. */
export interface SettingsFilesystem {
  readText(path: string): Promise<string>;
  readBytes(path: string): Promise<Uint8Array>;
  /** Exclusively create and file-sync private bytes beside the publication target. */
  stageDurablePrivateBytes(target: string, bytes: Uint8Array): Promise<string>;
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
    const read = await this.#readHosts(read => this.#serialize(read));
    return {settings: read.value, problem: read.failure?.message ?? null};
  }

  async #readHosts(importQueue?: ImportQueue): Promise<ReadResult<SettingsFile>> {
    const read = await readDocument(
      this.#filesystem,
      this.#options.settingsPath,
      {
        decode: decodeSettings,
        encode: encodeSettings,
        legacy: value => decodeRetainedSettings(value),
      },
      importQueue,
    );
    if (read.kind === 'missing') {
      return {value: EMPTY_SETTINGS, failure: null};
    }
    if (read.kind === 'failed') {
      const failure = readFailure(read.path, read.error);
      this.#log(failure.message);
      return {value: EMPTY_SETTINGS, failure};
    }
    return {value: read.value, failure: null};
  }

  /** The recent runs, read afresh. */
  async recent(): Promise<RecentFile> {
    return (await this.#readRecent(read => this.#serialize(read))).value;
  }

  /** The recent runs and the entries left out of them, read afresh. */
  async recentSnapshot(): Promise<RecentSnapshot> {
    const read = await this.#readRecent(read => this.#serialize(read));
    return {recent: read.value, skipped: read.skipped};
  }

  async #readRecent(importQueue?: ImportQueue): Promise<RecentRead> {
    const read = await readDocument(
      this.#filesystem,
      this.#options.recentPath,
      {
        decode: decodeRecent,
        encode: encodeRecent,
        legacy: value => parseRecent(value),
      },
      importQueue,
    );
    if (read.kind === 'missing')
      return {value: EMPTY_RECENT, failure: null, skipped: [], invalid: []};
    if (read.kind === 'failed') {
      const failure = readFailure(read.path, read.error);
      this.#log(failure.message);
      return {value: EMPTY_RECENT, failure, skipped: [], invalid: []};
    }
    const {file, skipped, invalid} = read.value;
    if (skipped.length > 0) {
      const reasons = skipped.map(entry => entry.reason).join('; ');
      this.#log(`skipping ${skipped.length} entries of ${this.#options.recentPath}: ${reasons}`);
    }
    return {value: file, failure: null, skipped, invalid};
  }

  /** The preferences, read afresh; the defaults when the file is absent or invalid. */
  async preferences(): Promise<Preferences> {
    return (await this.#readPreferences(read => this.#serialize(read))).value;
  }

  async #readPreferences(importQueue?: ImportQueue): Promise<ReadResult<Preferences>> {
    const path = this.#options.preferencesPath;
    const read = await readDocument(
      this.#filesystem,
      path,
      {
        decode: decodePreferences,
        encode: encodePreferences,
        legacy: value => parsePreferences(value),
      },
      importQueue,
    );
    if (read.kind === 'missing') return {value: DEFAULT_PREFERENCES, failure: null};
    if (read.kind === 'failed') {
      const failure = readFailure(read.path, read.error);
      this.#log(failure.message);
      return {value: DEFAULT_PREFERENCES, failure};
    }
    return {value: read.value, failure: null};
  }

  /** Replace the preferences with `update` of the current ones, then tell the listeners. */
  updatePreferences(update: (preferences: Preferences) => Preferences): Promise<Preferences> {
    return this.#serialize(async () => {
      const read = await this.#readPreferences();
      if (read.failure !== null) throw read.failure;
      const next = update(read.value);
      await writeBytes(
        this.#filesystem,
        settingsBinaryPath(this.#options.preferencesPath),
        encodePreferences(next),
      );
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
      const outcome = await writeBytes(
        this.#filesystem,
        settingsBinaryPath(this.#options.settingsPath),
        encodeSettings(next),
        current,
      );
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
        (await writeBytes(
          this.#filesystem,
          settingsBinaryPath(this.#options.recentPath),
          encodeRecent(parseRecent(document)),
          current,
        )) === 'cancelled'
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

export class NodeSettingsFilesystem implements SettingsFilesystem {
  readonly #directorySync: DirectorySyncCapability;

  constructor(options: NodeSettingsFilesystemOptions = {}) {
    this.#directorySync = options.directorySync ?? nodeDirectorySync(process.platform);
  }

  readText(path: string): Promise<string> {
    return readFile(path, 'utf8');
  }

  readBytes(path: string): Promise<Uint8Array> {
    return readFile(path);
  }

  stageDurablePrivateBytes(target: string, bytes: Uint8Array): Promise<string> {
    return stagePrivateBytes(target, bytes, 0o600);
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
async function stagePrivateBytes(target: string, text: Uint8Array, mode: number): Promise<string> {
  let collision: unknown;
  for (let attempt = 0; attempt < MAX_STAGING_ATTEMPTS; attempt += 1) {
    const staging = `${target}.${randomUUID()}.tmp`;
    try {
      await writeNewPrivateBytes(staging, text, mode);
      return staging;
    } catch (error) {
      if (!hasCode(error, 'EEXIST')) throw error;
      collision = error;
    }
  }
  throw new Error(`could not create a private staging file beside ${target}`, {cause: collision});
}

async function writeNewPrivateBytes(path: string, text: Uint8Array, mode: number): Promise<void> {
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

interface SettingsCodec<T> {
  decode(bytes: Uint8Array): T;
  encode(value: T): Uint8Array;
  legacy(value: unknown): T;
}

type DocumentRead<T> =
  | {readonly kind: 'value'; readonly value: T}
  | {readonly kind: 'missing'}
  | {readonly kind: 'failed'; readonly error: unknown; readonly path: string};

type ImportQueue = <T>(read: () => Promise<DocumentRead<T>>) => Promise<DocumentRead<T>>;

/** Read published files immediately; serialize and recheck only missing-file imports. */
async function readDocument<T>(
  filesystem: SettingsFilesystem,
  path: string,
  codec: SettingsCodec<T>,
  importQueue?: ImportQueue,
): Promise<DocumentRead<T>> {
  const binaryPath = settingsBinaryPath(path);
  try {
    return {kind: 'value', value: codec.decode(await filesystem.readBytes(binaryPath))};
  } catch (error) {
    if (!hasCode(error, 'ENOENT')) return {kind: 'failed', error, path: binaryPath};
  }
  if (importQueue !== undefined) return importQueue(() => readDocument(filesystem, path, codec));
  let text: string;
  try {
    text = await filesystem.readText(path);
  } catch (error) {
    return hasCode(error, 'ENOENT') ? {kind: 'missing'} : {kind: 'failed', error, path};
  }
  try {
    const value = codec.legacy(JSON.parse(text) as unknown);
    await writeBytes(filesystem, binaryPath, codec.encode(value));
    return {kind: 'value', value};
  } catch (error) {
    return {kind: 'failed', error, path};
  }
}

function hasCode(error: unknown, code: string): boolean {
  return typeof error === 'object' && error !== null && 'code' in error && error.code === code;
}

function readFailure(path: string, cause: unknown): Error {
  const detail = cause instanceof Error ? cause.message : String(cause);
  return new Error(`could not read ${path}: ${detail}`, {cause});
}

/** Publish validated bytes through the settings owner's existing durability boundary. */
async function writeBytes(
  filesystem: SettingsFilesystem,
  path: string,
  bytes: Uint8Array,
  current: () => boolean = () => true,
): Promise<SettingsUpdateOutcome> {
  const staging = await filesystem.stageDurablePrivateBytes(path, bytes);
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
