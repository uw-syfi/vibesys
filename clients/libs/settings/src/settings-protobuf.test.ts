import {describe, expect, test} from 'bun:test';
import {readFileSync} from 'node:fs';
import {
  decodePreferences,
  decodeRecent,
  decodeRetainedPreferences,
  decodeRetainedRecent,
  decodeRetainedSettings,
  decodeSettings,
  encodePreferences,
  encodeRecent,
  encodeSettings,
  SettingsStore,
} from './index.js';
import {FakeSettingsFilesystem, fileError} from './testing/index.js';

interface Fixture {
  readonly id: string;
  readonly kind: 'hosts' | 'preferences' | 'recent';
  readonly source: unknown;
  readonly protobufHex: string;
  readonly expected: unknown;
  readonly retainedProtobufHex?: string;
}
const fixtures = JSON.parse(
  readFileSync(
    new URL('../../../../proto/testdata/desktop/settings.json', import.meta.url),
    'utf8',
  ),
) as Fixture[];
const paths = {
  hosts: '/settings/hosts.json',
  preferences: '/settings/preferences.json',
  recent: '/settings/recent.json',
};
const binaryPaths = {
  hosts: '/settings/hosts.pb',
  preferences: '/settings/preferences.pb',
  recent: '/settings/recent.pb',
};
const codecs = {
  hosts: {read: decodeSettings, legacy: decodeRetainedSettings, encode: encodeSettings},
  preferences: {
    read: decodePreferences,
    legacy: decodeRetainedPreferences,
    encode: encodePreferences,
  },
  recent: {read: decodeRecent, legacy: decodeRetainedRecent, encode: encodeRecent},
};
function store(filesystem: FakeSettingsFilesystem): SettingsStore {
  return new SettingsStore({
    settingsPath: paths.hosts,
    preferencesPath: paths.preferences,
    recentPath: paths.recent,
    filesystem,
    log: () => {},
  });
}
async function read(settings: SettingsStore, kind: Fixture['kind']): Promise<unknown> {
  if (kind === 'hosts') return (await settings.hosts()).settings;
  if (kind === 'preferences') return settings.preferences();
  return settings.recent();
}

describe('desktop settings protobuf migration', () => {
  for (const fixture of fixtures) {
    test(`retained fixture ${fixture.id} decodes, publishes once and preserves its source`, async () => {
      const path = paths[fixture.kind];
      const source = `${JSON.stringify(fixture.source)}\n`;
      const filesystem = new FakeSettingsFilesystem({[path]: source});
      const settings = store(filesystem);
      const first = await read(settings, fixture.kind);
      const bytes = filesystem.bytes(binaryPaths[fixture.kind]);
      expect(Buffer.from(bytes).toString('hex')).toBe(fixture.protobufHex);
      expect<unknown>(codecs[fixture.kind].read(bytes)).toEqual(fixture.expected);
      expect(filesystem.text(path)).toBe(source);
      const before = filesystem.operations.length;
      // A changed retained source cannot override an already migrated file.
      filesystem.setText(path, '{"unknown":true}');
      expect(await read(store(filesystem), fixture.kind)).toEqual(first);
      expect(filesystem.operations.slice(before)).toEqual([
        {kind: 'read', path: binaryPaths[fixture.kind]},
      ]);
      filesystem.crash();
      expect(await read(store(filesystem), fixture.kind)).toEqual(first);
    });
  }

  test('unknown retained keys are refused with their path and never published', async () => {
    const cases = [
      {
        kind: 'hosts' as const,
        value: {version: 2, hosts: {}, unexpected: true},
        path: 'unexpected',
      },
      {
        kind: 'hosts' as const,
        value: {version: 2, hosts: {local: {checkout: '/project', unexpected: true}}},
        path: 'hosts.local.unexpected',
      },
      {
        kind: 'preferences' as const,
        value: {
          version: 1,
          appearance: {mode: 'system', light: 'paper', dark: 'ink', unexpected: true},
          notifications: {enabled: true},
          archived: [],
        },
        path: 'appearance.unexpected',
      },
    ];
    for (const item of cases) {
      const source = JSON.stringify(item.value);
      const filesystem = new FakeSettingsFilesystem({[paths[item.kind]]: source});
      const settings = store(filesystem);
      await read(settings, item.kind);
      const update =
        item.kind === 'hosts'
          ? settings.updateHosts(value => value)
          : settings.updatePreferences(value => value);
      await expect(update).rejects.toThrow(item.path);
      expect(filesystem.text(paths[item.kind])).toBe(source);
      expect(() => filesystem.bytes(binaryPaths[item.kind])).toThrow('ENOENT');
    }
  });

  test('a corrupt protobuf or denied protobuf read never falls back to retained JSON', async () => {
    for (const denied of [false, true]) {
      const source = JSON.stringify({version: 2, hosts: {local: {checkout: '/retained'}}});
      const filesystem = new FakeSettingsFilesystem({[paths.hosts]: source});
      filesystem.setBytes(binaryPaths.hosts, new Uint8Array([255]));
      if (denied)
        filesystem.failNextRead(binaryPaths.hosts, fileError('EACCES', binaryPaths.hosts));
      const observed = await store(filesystem).hosts();
      expect(observed.problem).toContain(binaryPaths.hosts);
      expect(filesystem.operations).toEqual([{kind: 'read', path: binaryPaths.hosts}]);
      expect(filesystem.text(paths.hosts)).toBe(source);
    }
  });

  test('unknown protobuf fields are rejected for every retained settings format', () => {
    for (const fixture of fixtures) {
      const bytes = new Uint8Array([...Buffer.from(fixture.protobufHex, 'hex'), 0x98, 0x06, 1]);
      expect(() => codecs[fixture.kind].read(bytes)).toThrow('#99');
    }
  });

  test('an import held at publication cannot overwrite an update queued after it', async () => {
    const filesystem = new FakeSettingsFilesystem({
      [paths.hosts]: JSON.stringify({version: 2, hosts: {local: {checkout: '/old'}}}),
    });
    const settings = store(filesystem);
    const gate = filesystem.deferNextWrite();
    const importing = settings.hosts();
    await gate.received;
    const updating = settings.updateHosts(current => ({
      ...current,
      hosts: {local: {checkout: '/new'}},
    }));
    gate.release();
    expect((await importing).settings.hosts['local']?.checkout).toBe('/old');
    await updating;
    expect((await store(filesystem).hosts()).settings.hosts['local']?.checkout).toBe('/new');
  });

  test('a published update is readable while its completion remains held', async () => {
    const filesystem = new FakeSettingsFilesystem();
    const settings = store(filesystem);
    await settings.updateHosts(() => ({version: 2, hosts: {local: {checkout: '/old'}}}));
    const gate = filesystem.deferNextReplaceAfterCommit();
    const updating = settings.updateHosts(() => ({version: 2, hosts: {local: {checkout: '/new'}}}));
    await gate.committed;
    expect((await settings.hosts()).settings.hosts['local']?.checkout).toBe('/new');
    gate.release();
    await updating;
  });

  test('each failed import boundary leaves the source intact and can be retried after recovery', async () => {
    for (const boundary of ['file', 'rename', 'directory'] as const) {
      const source = JSON.stringify({version: 2, hosts: {local: {checkout: '/old'}}});
      const filesystem = new FakeSettingsFilesystem({[paths.hosts]: source});
      const failure = fileError('EIO', binaryPaths.hosts);
      if (boundary === 'file') filesystem.failNextFileSync(binaryPaths.hosts, failure);
      else if (boundary === 'rename') filesystem.failNextReplace(binaryPaths.hosts, failure);
      else filesystem.failNextDirectorySync(binaryPaths.hosts, failure);
      expect((await store(filesystem).hosts()).problem).toContain('EIO');
      expect(filesystem.text(paths.hosts)).toBe(source);
      filesystem.crash();
      expect((await store(filesystem).hosts()).settings.hosts['local']?.checkout).toBe('/old');
      expect(filesystem.text(paths.hosts)).toBe(source);
    }
  });

  test('protobuf codecs preserve supported host destinations', () => {
    for (const alias of ['host-a', 'user@host-a:22', '::1', '_host', '.host']) {
      for (const checkout of ['~', '/', '~/project', '/project/é']) {
        const value = {version: 2 as const, hosts: {[`ssh:${alias}`]: {checkout}}};
        expect(decodeSettings(encodeSettings(value))).toEqual(value);
      }
    }
  });

  test('published historical host preferences load cleanly and disappear from the next write', async () => {
    const fixture = fixtures.find(value => value.retainedProtobufHex !== undefined);
    if (!fixture?.retainedProtobufHex) throw new Error('retained profile fixture is required');
    const bytes = Uint8Array.from(Buffer.from(fixture.retainedProtobufHex, 'hex'));
    const filesystem = new FakeSettingsFilesystem();
    filesystem.setBytes(binaryPaths.hosts, bytes);
    const settings = store(filesystem);
    const loaded = await settings.hosts();
    expect(loaded.problem).toBeNull();
    expect<unknown>(loaded.settings).toEqual(fixture.expected);
    expect(Buffer.from(encodeSettings(loaded.settings)).toString('hex')).toBe(fixture.protobufHex);
  });
});
