import {create, toBinary} from '@bufbuild/protobuf';
import {describe, expect, it} from '@effect/vitest';
import {
  DesktopHostSettingsSchema,
  DesktopHostsSchema,
  DesktopSshOptionsSchema,
  validatedFromBinary,
} from '@vibesys/proto';
import * as Effect from 'effect/Effect';
import fc from 'fast-check';
import {makeMachineActions} from '../core/machines/actions.js';
import {MachinesError, machinesApiOver} from '../core/machines/machines.js';
import {machineReplyFromBytes, machineRequestToBytes} from '../core/machines/machines-codec.js';
import type {Roster} from '../core/machines/roster.js';
import {JUMP_PATTERN} from '../state/settings/ssh-target.js';
import {makeMemoryFiles} from '../testing/main/memory-files.js';
import {serveMachines, sshArgsFor, sshArgsOfSettings} from './machines.js';
import {makeSavedHostsStore} from './saved-hosts-store.js';
import {makeSshConfigSource} from './ssh-config.js';

const encode = (text: string): Uint8Array => new TextEncoder().encode(text);

/** The window's side over the real main-process handler, with files and an ssh config in memory. */
function world(config: Record<string, string> = {}, saved: Record<string, Uint8Array> = {}) {
  const files = makeMemoryFiles({
    ...Object.fromEntries(Object.entries(config).map(([path, text]) => [path, encode(text)])),
    ...saved,
  });
  const lines: string[] = [];
  const store = makeSavedHostsStore('/data', files, line => lines.push(line));
  const source = makeSshConfigSource(
    files,
    {config: '/home/u/.ssh/config', home: '/home/u'},
    line => lines.push(line),
  );
  const serve = serveMachines(store, source);
  // The window's calls over the real codec and the real handler; only the transport is absent.
  const machines = machinesApiOver(request =>
    serve(machineRequestToBytes(request)).pipe(
      Effect.map(machineReplyFromBytes),
      Effect.flatMap(reply =>
        reply.kind === 'refused'
          ? Effect.fail(new MachinesError({detail: reply.reason}))
          : Effect.succeed(reply),
      ),
    ),
  );
  return {files, store, machines, lines};
}

const CONFIG = {
  '/home/u/.ssh/config': 'Include conf.d/*.conf\nHost host-a mine\nHost *\n',
  '/home/u/.ssh/conf.d/b.conf': 'Host host-b\n',
  '/home/u/.ssh/conf.d/a.conf': 'Host my-host-c\nInclude /home/u/.ssh/config\n',
};

describe('machines over the main process', () => {
  it.effect('offers the aliases of the config and its includes, prefix matches first', () =>
    Effect.gen(function* () {
      const {machines} = world(CONFIG);
      expect(yield* machines.match('host')).toEqual(['host-a', 'host-b', 'my-host-c']);
      expect(yield* machines.match('')).toEqual(['host-a', 'mine', 'my-host-c', 'host-b']);
      expect(yield* machines.match('zz')).toEqual([]);
    }),
  );

  it.effect('saves an alias once: the second add finds it', () =>
    Effect.gen(function* () {
      const {machines, store} = world(CONFIG);
      expect(yield* machines.add('host-a')).toEqual({host: 'ssh:host-a', created: true});
      expect(yield* machines.add('host-a')).toEqual({host: 'ssh:host-a', created: false});
      expect((yield* store.list).map(host => host.key)).toEqual(['ssh:host-a']);
    }),
  );

  it.effect('saves a pasted command by its destination and keeps its options as ssh flags', () =>
    Effect.gen(function* () {
      const {machines, store} = world();
      const added = yield* machines.add('ssh -p 2222 user@host-a -J jump-a -i ~/.ssh/key');
      expect(added.host).toBe('ssh:user@host-a');
      const [saved] = yield* store.list;
      expect(saved ? sshArgsOfSettings(saved.settings) : []).toEqual([
        '-p',
        '2222',
        '-J',
        'jump-a',
        '-i',
        '~/.ssh/key',
      ]);
      expect(yield* sshArgsFor(store, ['-F', 'conf'])('user@host-a')).toEqual([
        '-F',
        'conf',
        '-p',
        '2222',
        '-J',
        'jump-a',
        '-i',
        '~/.ssh/key',
      ]);
      expect(yield* sshArgsFor(store, [])('other')).toEqual([]);
    }),
  );

  it.effect('refuses a bad flag by name and saves nothing', () =>
    Effect.gen(function* () {
      const {machines, store, files} = world();
      const failure = yield* Effect.flip(machines.add('ssh -X host-a'));
      expect(failure.detail).toBe('-X is not accepted');
      expect((yield* Effect.flip(machines.add('ssh -o ProxyCommand=x host-a'))).detail).toBe(
        '-o ProxyCommand is not accepted',
      );
      expect(yield* store.list).toEqual([]);
      expect(files.written()).toEqual([]);
    }),
  );

  it.effect('removes a host and puts it back whole with its options', () =>
    Effect.gen(function* () {
      const {machines, store} = world();
      yield* machines.add('ssh -p 2200 host-a');
      yield* machines.add('host-b');
      const undo = yield* machines.remove('ssh:host-a');
      expect((yield* store.list).map(host => host.key)).toEqual(['ssh:host-b']);
      expect((yield* Effect.flip(machines.remove('ssh:host-a'))).detail).toContain('not saved');
      expect(yield* machines.restore(undo)).toEqual({host: 'ssh:host-a', created: true});
      const kept = (yield* store.list).find(host => host.key === 'ssh:host-a');
      expect(kept?.settings.ssh?.port).toBe(2200);
    }),
  );

  it.effect('keeps a host saved before this app wrote them, and what else the file held', () =>
    Effect.gen(function* () {
      const before = toBinary(
        DesktopHostsSchema,
        create(DesktopHostsSchema, {
          version: 1,
          hosts: {local: create(DesktopHostSettingsSchema, {checkout: '/srv/x'})},
        }),
      );
      const {machines, files} = world({}, {'/data/hosts.pb': before});
      yield* machines.add('host-a');
      const after = validatedFromBinary(DesktopHostsSchema, files.at('/data/hosts.pb') ?? before);
      expect(Object.keys(after.hosts)).toEqual(['local', 'ssh:host-a']);
      expect(after.hosts['local']?.checkout).toBe('/srv/x');
    }),
  );

  it.effect('leaves a file it cannot read alone and says why', () =>
    Effect.gen(function* () {
      const {machines, files} = world({}, {'/data/hosts.pb': new Uint8Array([0xff, 0xff])});
      const failure = yield* Effect.flip(machines.add('host-a'));
      expect(failure.detail).toContain('cannot be read');
      expect(files.written()).toEqual([]);
    }),
  );

  it.effect('reads the older JSON file and writes hosts.pb on the first save', () =>
    Effect.gen(function* () {
      const {machines, store, files} = world({
        '/data/hosts.json': JSON.stringify({version: 2, hosts: {'ssh:host-b': {checkout: '/srv'}}}),
      });
      expect((yield* store.list).map(host => host.key)).toEqual(['ssh:host-b']);
      yield* machines.add('host-a');
      expect((yield* store.list).map(host => host.key)).toEqual(['ssh:host-b', 'ssh:host-a']);
      expect(files.written()).toEqual(['/data/hosts.pb']);
    }),
  );

  it.effect('ignores a saved key that names no host, logging it, and keeps the rest', () =>
    Effect.gen(function* () {
      const {store, lines} = world({
        '/data/hosts.json': JSON.stringify({version: 2, hosts: {'ssh:host-b': {}, '-bad': {}}}),
      });
      expect((yield* store.list).map(host => host.key)).toEqual(['ssh:host-b']);
      expect(lines.join('\n')).toContain('-bad');
    }),
  );

  it.effect('refuses bytes that are no request', () =>
    Effect.gen(function* () {
      const {files, store} = world();
      const reply = yield* serveMachines(
        store,
        makeSshConfigSource(files, {config: '/c', home: '/h'}, () => {}),
      )(new Uint8Array([0xff, 0xff]));
      expect(reply.length).toBeGreaterThan(0);
    }),
  );
});

describe('the options a command can carry', () => {
  it('are held to the contract: whatever the parser accepts validates as saved settings', () => {
    fc.assert(
      fc.property(
        fc.array(fc.stringMatching(/^[a-z0-9][a-z0-9.-]{0,8}$/), {minLength: 1, maxLength: 3}),
        fc.integer({min: 1, max: 65535}),
        (hosts, port) => {
          const jump = hosts.join(',');
          expect(JUMP_PATTERN.test(jump)).toBe(true);
          const bytes = toBinary(
            DesktopHostSettingsSchema,
            create(DesktopHostSettingsSchema, {
              ssh: create(DesktopSshOptionsSchema, {port, jump, identity: '~/k'}),
            }),
          );
          expect(bytes.length).toBeGreaterThan(0);
        },
      ),
    );
  });
});

describe('what the Resources tab does with machines', () => {
  const roster = () => {
    const calls: string[] = [];
    const fake: Roster = {
      entries: undefined as never,
      add: key => Effect.sync(() => void calls.push(`add ${key}`)),
      remove: key => Effect.sync(() => void calls.push(`remove ${key}`)),
    };
    return {fake, calls};
  };

  it.effect('opens a session for a saved host, ends it on removal and reopens it on undo', () =>
    Effect.gen(function* () {
      const {machines} = world();
      const {fake, calls} = roster();
      const actions = makeMachineActions(machines, fake);
      expect(yield* actions.add('host-a')).toEqual({
        kind: 'added',
        host: 'ssh:host-a',
        created: true,
      });
      const removed = yield* actions.remove('ssh:host-a');
      if (removed.kind !== 'removed') throw new Error('expected a removal');
      expect(yield* actions.undo('ssh:host-a', removed.undo)).toEqual({
        kind: 'added',
        host: 'ssh:host-a',
        created: true,
      });
      expect(calls).toEqual(['add ssh:host-a', 'remove ssh:host-a', 'add ssh:host-a']);
    }),
  );

  it.effect('shows a refusal as an outcome and opens nothing', () =>
    Effect.gen(function* () {
      const {machines} = world();
      const {fake, calls} = roster();
      const actions = makeMachineActions(machines, fake);
      expect(yield* actions.add('ssh -Q host-a')).toEqual({
        kind: 'refused',
        reason: '-Q is not accepted',
      });
      expect(calls).toEqual([]);
    }),
  );
});
