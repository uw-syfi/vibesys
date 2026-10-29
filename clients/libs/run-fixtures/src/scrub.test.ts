import {describe, expect, it} from 'bun:test';
import {Seeded} from '@vibesys/store/testing';
import {findLeaks, scrubText, scrubValue} from './index.js';

const POLICY = {users: ['alice', 'jsmith'], hosts: ['build-host', 'dev-box-07']};

/** Fragments that carry a leak, and what must not survive of each. */
const LEAKY: readonly (readonly [string, string])[] = [
  ['/home/alice/projects/queue-rs/src/lib.rs', 'alice'],
  ['/Users/jsmith/work/vibesys', 'jsmith'],
  ['/tmp/run-4821/out.log', '/tmp/run'],
  ['/mnt/scratch/shared/data.bin', '/mnt/scratch/shared'],
  ['C:\\Users\\alice\\project\\main.rs', 'alice'],
  ['ssh:build-host', 'build-host'],
  ['ssh:dev-box-07.cluster.internal', 'dev-box-07'],
  ['alice@build-host', 'build-host'],
  ['alice@corp.example.org', 'corp.example.org'],
  ['10.4.22.9', '10.4.22.9'],
  ['https://git.corp.internal/team/repo.git', 'corp.internal'],
  ['connected to BUILD-HOST as Alice', 'BUILD-HOST'],
];

/** Fragments with nothing to scrub: code, ratios, units, relative paths. */
const SAFE: readonly string[] = [
  'src/lib.rs',
  'ops/s',
  '1.18x',
  'N/A',
  'and/or',
  'cargo bench --bench queue',
  '@vibesys/run-fixtures',
  'https://example.com/docs',
  'http://localhost:8080/health',
  '0.9.0',
  '93 of 100 trials',
  'implementation-failed',
];

function sentence(random: Seeded, fragments: readonly string[]): string {
  const words = Array.from({length: 1 + random.int(5)}, () => random.pick(fragments));
  return words.join(random.pick([' ', ' then ', ': ', ', ', '\n', ' "', ' (']));
}

describe('scrubText', () => {
  it('removes every leak from any mix of leaky and safe text', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      const random = new Seeded(seed);
      const fragments = [...LEAKY.map(([text]) => text), ...SAFE];
      const text = sentence(random, fragments);
      const scrubbed = scrubText(text, POLICY);
      expect(findLeaks(scrubbed, POLICY)).toEqual([]);
      for (const [fragment, secret] of LEAKY) {
        if (text.includes(fragment)) {
          expect(scrubbed.toLowerCase()).not.toContain(secret.toLowerCase());
        }
      }
    }
  });

  it('is idempotent', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      const random = new Seeded(seed);
      const text = sentence(random, [...LEAKY.map(([fragment]) => fragment), ...SAFE]);
      const once = scrubText(text, POLICY);
      expect(scrubText(once, POLICY)).toBe(once);
    }
  });

  it('leaves text with nothing to scrub exactly as it was', () => {
    for (let seed = 1; seed <= 300; seed += 1) {
      const text = sentence(new Seeded(seed), SAFE);
      expect(scrubText(text, POLICY)).toBe(text);
      expect(findLeaks(text, POLICY)).toEqual([]);
    }
  });

  it('does not match a user or host name inside a longer word', () => {
    expect(scrubText('malice and build-host2 and alice-in', POLICY)).toBe(
      'malice and build-host2 and alice-in',
    );
  });
});

describe('scrubValue and findLeaks', () => {
  it('scrub every string and every key of a nested value, and name where a leak is', () => {
    const value = {
      '/home/alice/key': 1,
      list: ['ok', {inner: 'run on ssh:build-host'}],
      count: 3,
      nothing: null,
    };
    const leaks = findLeaks(value, POLICY);
    expect([...new Set(leaks.map(leak => leak.path))].sort()).toEqual(
      ['.<key /home/alice/key>', '.list[1].inner'].sort(),
    );
    const scrubbed = scrubValue(value, POLICY);
    expect(findLeaks(scrubbed, POLICY)).toEqual([]);
    expect(scrubbed.count).toBe(3);
    expect(scrubbed.nothing).toBeNull();
    expect(scrubbed.list[0]).toBe('ok');
  });
});
