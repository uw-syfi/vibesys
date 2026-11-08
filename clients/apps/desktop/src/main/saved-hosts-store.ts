/**
 * The hosts the user saved, kept in the app's user data so that the window opens on them: `hosts.pb`
 * (a `DesktopHosts` message of `proto/vibesys/desktop/v1/settings.proto`), or the older `hosts.json`
 * when it is the only file (read once; the first save writes `hosts.pb`). A file that cannot be
 * read or parsed lists no hosts and a log line naming it, so a damaged file never keeps the window
 * from opening, and a save never replaces such a file: it fails with the reason and leaves the
 * file alone. Saves run one at a time, in this process and, under the file's lock, across processes,
 * each a read-modify-write of the whole file, written whole by rename. A save that was cut short
 * leaves a staging file beside it, which the startup sweep (`sweepStaged`) removes.
 */
import {join} from 'node:path';
import {create, toBinary} from '@bufbuild/protobuf';
import {
  type DesktopHostSettings,
  DesktopHostSettingsSchema,
  type DesktopHosts,
  DesktopHostsSchema,
  validatedFromBinary,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import * as Semaphore from 'effect/Semaphore';
import {type HostKey, hostFromKey, hostKey} from '../state/host.js';
import type {FileLock, ReadFile, WriteFile} from './files.js';

const BINARY = 'hosts.pb';

/** Where the saved hosts are kept under the app's user data. */
export const savedHostsPath = (userData: string): string => join(userData, BINARY);
const JSON_FILE = 'hosts.json';

/** One saved host: its key and what is kept for it. */
interface SavedHost {
  readonly key: HostKey;
  readonly settings: DesktopHostSettings;
}

export interface SavedHostsStore {
  /** The saved hosts, valid ones only, in the order saved; none when nothing readable is saved. */
  readonly list: Effect.Effect<readonly SavedHost[]>;
  /** Saves the host unless it is already saved; true when it was new. Fails when the file cannot be replaced. */
  readonly add: (host: SavedHost) => Effect.Effect<boolean, Error>;
  /** Forgets the host and returns what was kept for it; null when it was not saved. */
  readonly remove: (key: HostKey) => Effect.Effect<SavedHost | null, Error>;
  /**
   * Keeps `directory` as the repositories folder of a saved host (empty clears it). This machine is
   * kept on first use; any other host must already be saved, else the call fails naming it.
   */
  readonly setRepositories: (key: HostKey, directory: string) => Effect.Effect<void, Error>;
}

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

/** The hosts of the older JSON form; its settings other than a checkout are not carried over. */
function hostsOfJson(text: string): DesktopHosts['hosts'] {
  const document: unknown = JSON.parse(text);
  if (!isRecord(document) || !isRecord(document['hosts'])) throw new Error('no hosts object');
  const hosts: DesktopHosts['hosts'] = {};
  for (const [key, value] of Object.entries(document['hosts'])) {
    const checkout = isRecord(value) ? value['checkout'] : undefined;
    hosts[key] = create(DesktopHostSettingsSchema, {
      checkout: typeof checkout === 'string' ? checkout : '',
    });
  }
  return hosts;
}

/** What the files hold: nothing, hosts (in the order the file keeps them), or a file this app cannot read. */
type Held =
  | {readonly kind: 'hosts'; readonly hosts: DesktopHosts['hosts']; readonly source: string}
  | {readonly kind: 'unreadable'; readonly cause: string; readonly source: string};

/** What the files hold now: `hosts.pb`, else the older `hosts.json`, else nothing. */
function heldHosts(
  binary: string,
  json: string,
  files: {readonly read: ReadFile},
): Effect.Effect<Held> {
  return Effect.gen(function* () {
    const bytes = yield* files.read(binary);
    if (bytes !== null) {
      try {
        return {
          kind: 'hosts',
          hosts: validatedFromBinary(DesktopHostsSchema, bytes).hosts,
          source: binary,
        } as const;
      } catch (cause) {
        return {kind: 'unreadable', cause: String(cause), source: binary} as const;
      }
    }
    const text = yield* files.read(json);
    if (text === null) return {kind: 'hosts', hosts: {}, source: binary} as const;
    try {
      return {
        kind: 'hosts',
        hosts: hostsOfJson(new TextDecoder().decode(text)),
        source: json,
      } as const;
    } catch (cause) {
      return {kind: 'unreadable', cause: String(cause), source: json} as const;
    }
  }).pipe(
    Effect.catch(error =>
      Effect.succeed({kind: 'unreadable', cause: error.message, source: binary} as const),
    ),
  );
}

export function makeSavedHostsStore(
  userData: string,
  files: {readonly read: ReadFile; readonly write: WriteFile; readonly lock: FileLock},
  log: (line: string) => void,
): SavedHostsStore {
  const binary = savedHostsPath(userData);
  const json = join(userData, JSON_FILE);
  const held = heldHosts(binary, json, files);
  /** One save at a time in this process, and under the file's lock across processes. */
  const saving = Semaphore.makeUnsafe(1);
  const locked = <A>(save: Effect.Effect<A, Error>) =>
    saving.withPermits(1)(files.lock(binary, save));
  /** The file as a save starts from it; one this app cannot read is refused, never replaced. */
  const beforeSave = Effect.gen(function* () {
    const before = yield* held;
    if (before.kind === 'unreadable') {
      const reason = `${before.source} cannot be read (${before.cause}); it is left as it is`;
      log(reason);
      return yield* Effect.fail(new Error(reason));
    }
    return before.hosts;
  });
  const save = (hosts: DesktopHosts['hosts']) =>
    Effect.gen(function* () {
      const message = create(DesktopHostsSchema, {version: 1, hosts});
      const bytes = toBinary(DesktopHostsSchema, message);
      // Never write what this app could not read back.
      yield* Effect.try({
        try: () => validatedFromBinary(DesktopHostsSchema, bytes),
        catch: cause => (cause instanceof Error ? cause : new Error(String(cause))),
      });
      yield* files.write(binary, bytes);
    });
  /** The settings as saved: only the fields this app writes, so a retired field never comes back. */
  const clean = (settings: DesktopHostSettings): DesktopHostSettings =>
    create(DesktopHostSettingsSchema, {
      checkout: settings.checkout,
      repositoriesDirectory: settings.repositoriesDirectory,
      ...(settings.ssh === undefined ? {} : {ssh: settings.ssh}),
    });
  return {
    list: Effect.gen(function* () {
      const file = yield* held;
      if (file.kind === 'unreadable') {
        log(`could not read the saved hosts: ${file.source}: ${file.cause}`);
        return [];
      }
      const valid: SavedHost[] = [];
      for (const [key, settings] of Object.entries(file.hosts)) {
        try {
          valid.push({key: hostKey(hostFromKey(key)), settings});
        } catch (cause) {
          log(`${file.source}: ignoring host ${JSON.stringify(key)}: ${String(cause)}`);
        }
      }
      return valid;
    }),
    add: host =>
      locked(
        Effect.gen(function* () {
          const hosts = yield* beforeSave;
          if (host.key in hosts) return false;
          yield* save({...hosts, [host.key]: clean(host.settings)});
          return true;
        }),
      ),
    remove: key =>
      locked(
        Effect.gen(function* () {
          const hosts = yield* beforeSave;
          const settings = hosts[key];
          if (settings === undefined) return null;
          const {[key]: _removed, ...rest} = hosts;
          yield* save(rest);
          return {key, settings};
        }),
      ),
    setRepositories: (key, directory) =>
      locked(
        Effect.gen(function* () {
          const hosts = yield* beforeSave;
          const settings = hosts[key];
          if (settings === undefined && key !== hostKey({kind: 'local'}))
            return yield* Effect.fail(new Error(`${key} is not saved`));
          yield* save({
            ...hosts,
            [key]: clean(
              create(DesktopHostSettingsSchema, {
                checkout: settings?.checkout ?? '',
                repositoriesDirectory: directory,
                ...(settings?.ssh === undefined ? {} : {ssh: settings.ssh}),
              }),
            ),
          });
        }),
      ),
  };
}
