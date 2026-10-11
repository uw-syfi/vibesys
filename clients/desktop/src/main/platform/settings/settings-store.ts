/**
 * The app's settings files, `hosts.json` (per-host checkouts) and `recent.json` (runs this user
 * opened), and their one writer.
 *
 * Guarantees:
 *
 * - **One writer.** Every update goes through this store, one at a time across both files, so an
 *   update always reads what the one before it wrote and none drops another's change. With the
 *   app's single-instance lock (`main.ts`), no other process writes these files.
 * - **Atomic replace.** The text goes to a sibling file first and is renamed over the target, so a
 *   reader sees the old file or the new one, never a partial write.
 * - **Changes are announced.** `onRecentChanged` listeners hear every update of the recent runs, so
 *   no view has a refresh path to forget.
 * - **Invalid files are ignored, not fatal**, and the problem is reported (`hostsProblem`).
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
}

export class SettingsStore {
  readonly #options: SettingsStoreOptions;
  /** The last update; each update starts after it settles. */
  #queue: Promise<void> = Promise.resolve();
  #hostsProblem: string | null = null;
  readonly #recentListeners = new Set<(recent: RecentFile) => void>();

  constructor(options: SettingsStoreOptions) {
    this.#options = options;
  }

  /** Why `hosts.json` was ignored on its last read; null when it is fine or absent. */
  get hostsProblem(): string | null {
    return this.#hostsProblem;
  }

  /** The host settings, read afresh. */
  async hosts(): Promise<SettingsFile> {
    let text: string;
    try {
      text = await readFile(this.#options.settingsPath, 'utf8');
    } catch {
      this.#hostsProblem = null;
      return EMPTY_SETTINGS;
    }
    try {
      const parsed = parseSettings(JSON.parse(text) as unknown);
      this.#hostsProblem = null;
      return parsed;
    } catch (error) {
      this.#hostsProblem = `${this.#options.settingsPath}: ${(error as Error).message}`;
      this.#options.log(`ignoring ${this.#hostsProblem}`);
      return EMPTY_SETTINGS;
    }
  }

  /** The recent runs, read afresh. */
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

  /** Replace the host settings with `update` of the current ones. */
  updateHosts(update: (current: SettingsFile) => SettingsFile): Promise<void> {
    return this.#serialize(async () => {
      await writeJson(this.#options.settingsPath, update(await this.hosts()));
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
      const next = update(await this.recent());
      if (!(await writeJson(this.#options.recentPath, next, current))) return;
      for (const listener of this.#recentListeners) listener(next);
    });
  }

  /** Hear every update of the recent runs; returns the unsubscribe. */
  onRecentChanged(listener: (recent: RecentFile) => void): () => void {
    this.#recentListeners.add(listener);
    return () => this.#recentListeners.delete(listener);
  }

  #serialize(write: () => Promise<void>): Promise<void> {
    const next = this.#queue.then(write);
    this.#queue = next.catch(() => undefined);
    return next;
  }
}

/** Replace `path` with `value` as JSON, private to the user, atomically (write aside, rename). */
async function writeJson(
  path: string,
  value: unknown,
  current: () => boolean = () => true,
): Promise<boolean> {
  const staging = `${path}.${process.pid}.tmp`;
  await writeFile(staging, `${JSON.stringify(value, null, 2)}\n`, {mode: 0o600});
  if (!current()) {
    await unlink(staging);
    return false;
  }
  await rename(staging, path);
  return true;
}
