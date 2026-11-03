import {hostKey} from '@vibesys/hosts';
import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {
  distinctHosts,
  hostsFromArgv,
  isAppPage,
  startupHosts,
  UI_PAGE,
  windowUrl,
} from './window-policy.js';

const alias = fc.stringMatching(/^[a-z][a-z0-9-]{0,10}$/);
const key = fc.oneof(
  fc.constant('local'),
  alias.map(name => `ssh:${name}`),
);

describe('the window’s address', () => {
  it('names the page and every host, and is recognised as an app page', () => {
    fc.assert(
      fc.property(fc.array(key, {maxLength: 4}), keys => {
        const hosts = distinctHosts(keys);
        const url = new URL(windowUrl('/app/dist/ui', hosts));
        expect(url.pathname).toBe(`/app/dist/ui/${UI_PAGE}`);
        expect(url.searchParams.getAll('host')).toEqual(hosts);
        expect(isAppPage('/app/dist/ui', url.href)).toBe(true);
      }),
    );
  });

  it('is not confused with any other file or site', () => {
    for (const other of [
      'https://example.test/index.html',
      'file:///app/dist/ui/other.html',
      'file:///elsewhere/index.html',
      'not a url',
      '',
    ]) {
      expect(isAppPage('/app/dist/ui', other)).toBe(false);
    }
  });
});

describe('the hosts a window opens on', () => {
  it('are the flags when given, else This Mac then the saved hosts, each once', () => {
    fc.assert(
      fc.property(fc.array(key, {maxLength: 4}), fc.array(key, {maxLength: 4}), (flags, saved) => {
        const asked = hostsFromArgv(flags.map(value => `--host=${value}`));
        const shown = startupHosts(asked, saved);
        expect(new Set(shown).size).toBe(shown.length);
        if (flags.length > 0) expect(shown).toEqual(distinctHosts(flags));
        else expect(shown[0]).toBe(hostKey({kind: 'local'}));
      }),
    );
  });

  it('refuse a flag that names no host, naming it', () => {
    expect(() => hostsFromArgv(['--host=nowhere'])).toThrow('nowhere');
  });
});
