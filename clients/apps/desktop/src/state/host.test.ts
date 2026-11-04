import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {HostValidationError, hostFromKey, hostKey, hostLabel, validAlias} from './host.js';

const ALIAS_CHARS = 'abcXYZ019_.@:-';
const alias = fc
  .tuple(
    fc.constantFrom(...'abcXYZ019_.@:'),
    fc.array(fc.constantFrom(...ALIAS_CHARS), {maxLength: 40}),
  )
  .map(([first, rest]) => first + rest.join(''));

describe('host keys', () => {
  it('name this machine as local and a destination as ssh:<alias>', () => {
    expect(hostKey({kind: 'local'})).toBe('local');
    expect(hostKey({kind: 'ssh', alias: 'host-a'})).toBe('ssh:host-a');
    expect(hostLabel({kind: 'local'})).toBe('This Mac');
    expect(hostLabel({kind: 'ssh', alias: 'host-a'})).toBe('host-a');
  });

  it('read back as the host they were made from, for every usable alias', () => {
    fc.assert(
      fc.property(alias, name => {
        const id = {kind: 'ssh' as const, alias: name};
        expect(hostFromKey(hostKey(id))).toEqual(id);
      }),
    );
  });

  it('refuse an alias that could start an ssh option or break a command line', () => {
    fc.assert(
      fc.property(fc.string({maxLength: 20}), tail => {
        expect(() => validAlias(`-${tail}`)).toThrow(HostValidationError);
        expect(() => validAlias(`a ${tail}`)).toThrow(HostValidationError);
      }),
    );
    expect(() => validAlias('')).toThrow(HostValidationError);
    expect(() => validAlias('a'.repeat(256))).toThrow(HostValidationError);
  });

  it('name no host for anything that is not a key', () => {
    fc.assert(
      fc.property(
        fc.oneof(
          fc.string({maxLength: 20}),
          fc.integer(),
          fc.constant(undefined),
          fc.constant(null),
        ),
        value => {
          fc.pre(value !== 'local' && !(typeof value === 'string' && value.startsWith('ssh:')));
          expect(() => hostFromKey(value)).toThrow(HostValidationError);
        },
      ),
    );
  });
});
