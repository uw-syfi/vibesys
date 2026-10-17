import {describe, expect, test} from 'bun:test';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {parseExternalUrl, runOfWindow, runWindowUrl} from './windows.js';

const HOSTS = ['local', 'ssh:gpu-box', 'ssh:user@cluster.example', 'ssh:a.b_c-d'];
const NAME_CHARS = 'abcdefghijklmnopqrstuvwxyz0123456789._-';

function runId(random: Seeded): string {
  const first = NAME_CHARS[random.int(36)] ?? 'a';
  const rest = Array.from({length: random.int(40)}, () => NAME_CHARS[random.int(39)] ?? 'a');
  return [first, ...rest].join('');
}

describe('run window URLs', () => {
  test('property: the page a run window loads names exactly that run', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const run = {host: HOSTS[random.int(HOSTS.length)] ?? 'local', runId: runId(random)};
      const url = runWindowUrl('app://vibesys/shell.html', run);
      expect(url.startsWith('app://vibesys/shell.html?')).toBe(true);
      expect(runOfWindow(url.slice(url.indexOf('?')))).toEqual(run);
    }
  });

  test('the main window has no run; a malformed query names what is wrong', () => {
    expect(runOfWindow('')).toBeNull();
    expect(runOfWindow('?')).toBeNull();
    expect(() => runOfWindow('?host=local&run=r1&tab=logs')).toThrow('"tab" is not a known');
    expect(() => runOfWindow('?host=local&host=local&run=r1')).toThrow('host is given twice');
    expect(() => runOfWindow('?host=local')).toThrow('run window.runId must be a run id');
    expect(() => runOfWindow('?host=nowhere&run=r1')).toThrow('run window.host');
    expect(() => runOfWindow('?host=local&run=Bad%20Id')).toThrow('runId must be a run id');
  });
});

const SCHEMES = [
  'http',
  'https',
  'mailto',
  'javascript',
  'file',
  'data',
  'ftp',
  'app',
  'vbscript',
  '',
];
const PIECES = [
  'a',
  'b.example',
  '/p',
  '?q=1',
  '#f',
  ' ',
  '\n',
  ':',
  '//',
  '@',
  '%00',
  'x y',
  '\\',
];

function arbitraryAddress(random: Seeded): string {
  const scheme = SCHEMES[random.int(SCHEMES.length)] ?? 'https';
  const body = Array.from({length: random.int(6)}, () => PIECES[random.int(PIECES.length)]).join(
    '',
  );
  const shown = random.int(3) === 0 ? scheme.toUpperCase() : scheme;
  return `${shown}${random.int(4) === 0 ? '' : ':'}${body}`;
}

describe('addresses opened outside the app', () => {
  test('property: an accepted address is a web or mail address as written, with no space or control character', () => {
    let accepted = 0;
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      for (let run = 0; run < 400; run += 1) {
        const input = arbitraryAddress(random);
        let url: string | null = null;
        try {
          url = parseExternalUrl(input, 'url');
        } catch {
          url = null;
        }
        if (url === null) continue;
        accepted += 1;
        expect(url).toBe(input);
        expect(/^(?:https?:\/\/|mailto:)/i.test(url)).toBe(true);
        expect(/[\s\p{Cc}]/u.test(url)).toBe(false);
      }
    }
    expect(accepted).toBeGreaterThan(0);
  });

  test('web and mail addresses pass; scripts, files, other schemes and non-strings are refused by name', () => {
    for (const accepted of [
      'https://example.com/a?b=1',
      'HTTP://Example.com',
      'http://localhost:3000/#x',
      'mailto:a@b.co',
    ]) {
      expect(parseExternalUrl(accepted, 'url')).toBe(accepted);
    }
    for (const refused of [
      'javascript:alert(1)',
      'javascript://example.com/%0aalert(1)',
      'file:///etc/passwd',
      'data:text/html,x',
      'app://vibesys/shell.html',
      'ftp://example.com',
      'https:example.com',
      'https:///nohost',
      'https://exa mple.com',
      'https://example.com/\nx',
      '//example.com',
      './local.md',
      'mailto:',
      '',
    ]) {
      expect(() => parseExternalUrl(refused, 'url')).toThrow('url must be');
    }
    for (const refused of [null, 1, {}, ['https://example.com']]) {
      expect(() => parseExternalUrl(refused, 'url')).toThrow('url must be');
    }
    expect(() => parseExternalUrl(`https://example.com/${'a'.repeat(3000)}`, 'url')).toThrow(
      'at most 2048',
    );
  });
});
