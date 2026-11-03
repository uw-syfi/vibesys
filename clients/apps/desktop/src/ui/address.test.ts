import fc from 'fast-check';
import {describe, expect, it} from 'vitest';
import {hostsOfAddress} from './address.js';

const ALIAS = fc.stringMatching(/^[a-z][a-z0-9-]{0,12}$/);

describe('the hosts of a window address', () => {
  it('names each distinct host once, in the order given', () => {
    fc.assert(
      fc.property(fc.array(ALIAS, {maxLength: 6}), aliases => {
        const search = `?${aliases.map(alias => `host=ssh:${alias}`).join('&')}`;
        const keys = hostsOfAddress(search).map(host => host.key);
        expect(keys).toEqual([...new Set(aliases.map(alias => `ssh:${alias}`))]);
      }),
    );
  });

  it('is empty when the address names no host, and refuses a key that names none', () => {
    expect(hostsOfAddress('')).toEqual([]);
    expect(() => hostsOfAddress('?host=nonsense')).toThrow();
  });

  it('labels this machine', () => {
    expect(hostsOfAddress('?host=local').map(host => host.key)).toEqual(['local']);
  });
});
