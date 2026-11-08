/**
 * Serves the window's requests about machines: the ssh config aliases that match what was typed, an
 * add (an alias, or a pasted ssh command that is parsed and never run), a remove that says how to
 * put the host back, and that restore. A host's key is made from its destination; the options of a
 * pasted command (port, jump host, identity file) are kept beside it in the saved hosts, and
 * `sshArgsFor` turns them into the flags ssh takes whenever a link to the host is made.
 */
import {create} from '@bufbuild/protobuf';
import {
  type DesktopHostSettings,
  DesktopHostSettingsSchema,
  DesktopMachineRestoreSchema,
  DesktopSshOptionsSchema,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import {
  MACHINES_CHANNEL,
  type MachineReply,
  machineReplyToBytes,
  machineRequestFromBytes,
} from '../core/machines/machines-codec.js';
import {type HostKey, hostFromKey, hostKey} from '../state/host.js';
import {matchAliases} from '../state/settings/ssh-config.js';
import {type SshTarget, sshArgs, targetFromInput} from '../state/settings/ssh-target.js';
import {isOwnWindow, type OwnWindows, type PortEvent} from './port-ipc.js';
import type {SavedHostsStore} from './saved-hosts-store.js';
import type {SshConfigSource} from './ssh-config.js';

/** What is kept for a host besides its key: the options of its target, when it has any. */
function settingsOfTarget(target: SshTarget): DesktopHostSettings {
  const hasOptions =
    target.port !== undefined || target.jump !== undefined || target.identityFile !== undefined;
  return create(DesktopHostSettingsSchema, {
    ...(hasOptions
      ? {
          ssh: create(DesktopSshOptionsSchema, {
            ...(target.port === undefined ? {} : {port: target.port}),
            jump: target.jump ?? '',
            identity: target.identityFile ?? '',
          }),
        }
      : {}),
  });
}

/** The ssh flags a saved host's options stand for; none for an alias of the person's ssh config. */
export function sshArgsOfSettings(settings: DesktopHostSettings): string[] {
  const {ssh} = settings;
  if (ssh === undefined) return [];
  return sshArgs({
    destination: 'unused',
    ...(ssh.port === undefined ? {} : {port: ssh.port}),
    ...(ssh.jump === '' ? {} : {jump: ssh.jump}),
    ...(ssh.identity === '' ? {} : {identityFile: ssh.identity}),
  });
}

/** The flags of the saved host with this ssh destination; none when it is not saved. */
export const sshArgsFor =
  (store: SavedHostsStore, explicitConfig: readonly string[]) =>
  (alias: string): Effect.Effect<readonly string[]> =>
    Effect.map(store.list, hosts => {
      const key = hostKey({kind: 'ssh', alias});
      const saved = hosts.find(host => host.key === key);
      return [...explicitConfig, ...(saved === undefined ? [] : sshArgsOfSettings(saved.settings))];
    });

/** The repositories folder of each saved machine that has one, by host key. */
const folders = (store: SavedHostsStore): Effect.Effect<Record<string, string>> =>
  Effect.map(store.list, hosts =>
    Object.fromEntries(
      hosts
        .filter(host => host.settings.repositoriesDirectory !== '')
        .map(host => [host.key, host.settings.repositoriesDirectory]),
    ),
  );

const refused = (reason: string): MachineReply => ({kind: 'refused', reason});
const reasonOf = (error: unknown): string =>
  error instanceof Error ? error.message : String(error);

/** The reply to one request's bytes; a request that is no valid message is refused, never thrown. */
export const serveMachines =
  (store: SavedHostsStore, config: SshConfigSource) =>
  (bytes: Uint8Array): Effect.Effect<Uint8Array> =>
    Effect.gen(function* () {
      let reply: MachineReply;
      try {
        const request = machineRequestFromBytes(bytes);
        switch (request.op) {
          case 'match':
            reply = {
              kind: 'matches',
              aliases: matchAliases(yield* config.aliases, request.query),
            };
            break;
          case 'add': {
            const target = targetFromInput(request.input);
            const key: HostKey = hostKey({kind: 'ssh', alias: target.destination});
            const created = yield* store.add({key, settings: settingsOfTarget(target)});
            reply = {kind: 'added', host: key, created};
            break;
          }
          case 'remove': {
            hostFromKey(request.host);
            const removed = yield* store.remove(request.host);
            reply =
              removed === null
                ? refused(`${request.host} is not saved`)
                : {
                    kind: 'removed',
                    undo: create(DesktopMachineRestoreSchema, {
                      host: removed.key,
                      settings: removed.settings,
                    }),
                  };
            break;
          }
          case 'restore': {
            const created = yield* store.add({
              key: request.undo.host,
              settings: request.undo.settings ?? create(DesktopHostSettingsSchema),
            });
            reply = {kind: 'added', host: request.undo.host, created};
            break;
          }
          case 'repositories':
            reply = {kind: 'repositories', directories: yield* folders(store)};
            break;
          case 'set-repositories':
            yield* store.setRepositories(request.host, request.directory);
            reply = {kind: 'repositories', directories: yield* folders(store)};
            break;
        }
      } catch (error) {
        reply = refused(reasonOf(error));
      }
      return machineReplyToBytes(reply);
    }).pipe(
      // A failed read or save is a refusal the person reads, with the file's own reason.
      Effect.catch(error => Effect.sync(() => machineReplyToBytes(refused(reasonOf(error))))),
    );

/** What `installMachinesIpc` uses of Electron's `ipcMain`. */
export interface MachinesIpc {
  handle(
    channel: string,
    listener: (event: Pick<PortEvent, 'sender' | 'senderFrame'>, value: unknown) => unknown,
  ): unknown;
}

/** Serves the channel to the app's own windows only: any other sender is refused before a file is touched. */
export function installMachinesIpc(
  ipc: MachinesIpc,
  {
    serve,
    own,
    run,
  }: {
    readonly serve: (bytes: Uint8Array) => Effect.Effect<Uint8Array>;
    readonly own: OwnWindows;
    readonly run: <A>(effect: Effect.Effect<A, Error>) => Promise<A>;
  },
): void {
  ipc.handle(MACHINES_CHANNEL, (event, value) => {
    if (!isOwnWindow(event, own)) return run(Effect.fail(new Error('not an app-owned window')));
    if (!(value instanceof Uint8Array))
      return run(Effect.fail(new Error('a machines request must be protobuf bytes')));
    return run(serve(value));
  });
}
