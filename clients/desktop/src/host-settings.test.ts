import {describe, expect, test} from 'bun:test';
import {
  DEFAULT_HOST_SETTINGS,
  EMPTY_SETTINGS,
  parseSettings,
  SettingsError,
  settingsFor,
  sshConfigHosts,
  validAlias,
  withHostSettings,
} from './host-settings.js';

describe('sshConfigHosts', () => {
  test('lists concrete aliases in order, skipping patterns, comments, and duplicates', () => {
    const config = [
      '# my hosts',
      'Host gpu-box cluster.example.org',
      '  HostName 192.0.2.10',
      'Host *.internal !bastion',
      'host=jump # inline comment',
      'Match host other',
      'Host gpu-box',
      'Host *',
      '    User someone',
      'HostName not-a-host-line',
    ].join('\n');
    expect(sshConfigHosts(config)).toEqual(['gpu-box', 'cluster.example.org', 'jump']);
  });

  test('never offers a name that ssh would read as an option', () => {
    expect(sshConfigHosts('Host -oProxyCommand=evil ok')).toEqual(['ok']);
    expect(() => validAlias('-oProxyCommand=x')).toThrow(SettingsError);
    expect(() => validAlias('a b')).toThrow(SettingsError);
  });
});

describe('host settings', () => {
  test('a host without settings runs `vibesys`', () => {
    expect(settingsFor(EMPTY_SETTINGS, 'gpu-box')).toEqual(DEFAULT_HOST_SETTINGS);
  });

  test('settings round-trip through JSON and normalize whitespace', () => {
    const file = withHostSettings(EMPTY_SETTINGS, 'gpu-box', {
      vibesysCommand: 'uv run --project ~/src/vibesys vibesys',
    });
    expect(parseSettings(JSON.parse(JSON.stringify(file)))).toEqual(file);
    expect(
      parseSettings({version: 1, hosts: {a: {vibesysCommand: '  uv   run vibesys '}}}).hosts['a'],
    ).toEqual({vibesysCommand: 'uv run vibesys'});
  });

  test('rejects unknown keys and unusable values, naming the path', () => {
    const cases: [unknown, string][] = [
      [{version: 2, hosts: {}}, 'version must be 1'],
      [{version: 1, hosts: {}, extra: 1}, 'extra is not a known setting'],
      [{version: 1, hosts: {a: {vibesysCommand: 'x', shell: 'y'}}}, 'hosts.a.shell'],
      [{version: 1, hosts: {a: {vibesysCommand: ''}}}, 'hosts.a.vibesysCommand must not be empty'],
      [{version: 1, hosts: {a: {vibesysCommand: 'x\nrm -rf ~'}}}, 'must be one line'],
      [{version: 1, hosts: {a: {vibesysCommand: '-oProxyCommand=x'}}}, 'must start with a command'],
      [{version: 1, hosts: {'-x': {vibesysCommand: 'vibesys'}}}, 'not a usable ssh host name'],
      [{version: 1, hosts: {a: {}}}, 'hosts.a.vibesysCommand must be a string'],
    ];
    for (const [value, message] of cases) {
      expect(() => parseSettings(value)).toThrow(message);
    }
  });
});
