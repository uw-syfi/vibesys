import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {
  parseSshCommand,
  printSshCommand,
  SshCommandError,
  type SshTarget,
  sshArgs,
  targetFromInput,
} from './ssh-target.js';

const name = fc
  .tuple(
    fc.constantFrom(...'abcxyz019'),
    fc.array(fc.constantFrom(...'abcxyz019_.-'), {maxLength: 12}),
  )
  .map(([first, rest]) => first + rest.join(''));

const target: fc.Arbitrary<SshTarget> = fc
  .record(
    {
      destination: fc.oneof(
        name,
        fc.tuple(name, name).map(([user, host]) => `${user}@${host}`),
      ),
      port: fc.integer({min: 1, max: 65535}),
      jump: fc.array(name, {minLength: 1, maxLength: 3}).map(hosts => hosts.join(',')),
      identityFile: fc
        .array(fc.constantFrom(...'ab c/~._-\'"'), {minLength: 1, maxLength: 20})
        .map(chars => `~/${chars.join('')}`),
    },
    {requiredKeys: ['destination']},
  )
  .map(record => record as SshTarget);

const refusal = (text: string): string => {
  try {
    parseSshCommand(text);
  } catch (error) {
    if (error instanceof SshCommandError) return error.message;
    throw error;
  }
  return 'accepted';
};

describe('an ssh command', () => {
  it('reads the destination and the closed set of options', () => {
    expect(parseSshCommand('ssh -p 2222 user@host-a -J jump-a')).toEqual({
      destination: 'user@host-a',
      port: 2222,
      jump: 'jump-a',
    });
    expect(
      parseSshCommand('ssh -l bob -oPort=22 -o "ProxyJump jump-a" -i ~/.ssh/key host-a'),
    ).toEqual({destination: 'bob@host-a', port: 22, jump: 'jump-a', identityFile: '~/.ssh/key'});
  });

  it('prints and reads back as the same target, for any target', () => {
    fc.assert(
      fc.property(target, value => {
        expect(parseSshCommand(printSshCommand(value))).toEqual(value);
      }),
    );
  });

  it('turns the options into the flags of the same name and never a shell string', () => {
    fc.assert(
      fc.property(target, value => {
        const args = sshArgs(value);
        expect(args.filter(arg => arg === '-p')).toHaveLength(value.port === undefined ? 0 : 1);
        if (value.identityFile !== undefined) expect(args).toContain(value.identityFile);
        expect(args).not.toContain(value.destination);
      }),
    );
  });

  it('refuses any flag outside the set, naming it', () => {
    const letters = 'abcdefghijklmnoqrstuvwxyzABCDEFGHIKLMNOPQRSTUVWXYZ0123456789'
      .split('')
      .filter(letter => !'plJio'.includes(letter));
    fc.assert(
      fc.property(fc.constantFrom(...letters), name, (letter, host) => {
        expect(refusal(`ssh -${letter} ${host}`)).toBe(`-${letter} is not accepted`);
        expect(refusal(`ssh ${host} -${letter}`)).toBe(`-${letter} is not accepted`);
      }),
    );
    expect(refusal('ssh -o ProxyCommand=evil host-a')).toBe('-o ProxyCommand is not accepted');
    expect(refusal('ssh -oStrictHostKeyChecking=no host-a')).toBe(
      '-o StrictHostKeyChecking is not accepted',
    );
  });

  it('refuses shell syntax, a remote command, a repeat, a bad port and a missing destination', () => {
    expect(refusal('ssh host-a; rm -rf x')).toBe('shell syntax ";" is not accepted');
    expect(refusal('ssh host-a | cat')).toBe('shell syntax "|" is not accepted');
    expect(refusal('ssh host-a ls')).toBe('a remote command (ls) is not accepted');
    expect(refusal('ssh -p 1 -p 2 host-a')).toBe('-p is given twice');
    expect(refusal('ssh -p 99999 host-a')).toContain('-p 99999');
    expect(refusal('ssh -p 22')).toBe('the command has no destination');
    expect(refusal('ssh -l bob bob@host-a')).toContain('-l bob');
    expect(refusal('scp host-a')).toBe('not an ssh command');
    expect(refusal('ssh -i -x host-a')).toContain('-i -x');
    expect(refusal("ssh 'host-a")).toContain('quote');
    expect(refusal('ssh --help')).toBe('-- is not accepted');
  });

  it('takes a single word as an alias and refuses what cannot be one', () => {
    expect(targetFromInput(' host-a ')).toEqual({destination: 'host-a'});
    expect(() => targetFromInput('-oProxyCommand=x')).toThrow(SshCommandError);
    expect(() => targetFromInput('  ')).toThrow(SshCommandError);
    expect(targetFromInput('ssh host-a')).toEqual({destination: 'host-a'});
  });
});
