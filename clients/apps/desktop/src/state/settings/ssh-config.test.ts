import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {matchAliases, parseSshConfig} from './ssh-config.js';

const alias = fc
  .tuple(
    fc.constantFrom(...'abcXYZ019'),
    fc.array(fc.constantFrom(...'abcXYZ019_.-'), {maxLength: 10}),
  )
  .map(([first, rest]) => first + rest.join(''));

describe('an ssh config', () => {
  it('lists the Host aliases and Include paths, and skips patterns and comments', () => {
    const entries = parseSshConfig(
      [
        '# Host hidden',
        'Host host-a host-b',
        '  HostName 10.0.0.1',
        'host=host-c # note',
        'Host *',
        'Host dev-? !dev-x "quoted-d"',
        'Include conf.d/* ~/more',
        'Match host zzz',
      ].join('\n'),
    );
    expect(entries.aliases).toEqual(['host-a', 'host-b', 'host-c', 'quoted-d']);
    expect(entries.includes).toEqual(['conf.d/*', '~/more']);
  });

  it('never offers a pattern, whatever the Host line holds', () => {
    fc.assert(
      fc.property(
        fc.array(fc.oneof(alias, fc.constantFrom('*', 'a*', '?', '!a', 'a?b', '-o')), {
          maxLength: 8,
        }),
        words => {
          for (const found of parseSshConfig(`Host ${words.join(' ')}`).aliases) {
            expect(found).not.toMatch(/[*?!]/);
            expect(found.startsWith('-')).toBe(false);
          }
        },
      ),
    );
  });
});

describe('matching aliases', () => {
  it('ranks those that start with the query before those that contain it', () => {
    expect(matchAliases(['my-host-a', 'host-b', 'ahost'], 'host')).toEqual([
      'host-b',
      'my-host-a',
      'ahost',
    ]);
  });

  it('offers only aliases that contain the query, prefixes first, once each, within the limit', () => {
    fc.assert(
      fc.property(fc.array(alias, {maxLength: 20}), fc.string({maxLength: 4}), (aliases, query) => {
        const found = matchAliases(aliases, query, 5);
        const needle = query.trim().toLowerCase();
        expect(found.length).toBeLessThanOrEqual(5);
        expect(new Set(found).size).toBe(found.length);
        for (const one of found) expect(one.toLowerCase()).toContain(needle);
        const firstInside = found.findIndex(one => !one.toLowerCase().startsWith(needle));
        if (firstInside >= 0) {
          for (const one of found.slice(firstInside)) {
            expect(one.toLowerCase().startsWith(needle)).toBe(false);
          }
        }
      }),
    );
  });
});
