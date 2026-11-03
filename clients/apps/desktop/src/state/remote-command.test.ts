import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {commandOf, remoteCommand} from './remote-command.js';

/**
 * The invariant of the module comment: csh and tcsh expand history `!` even inside single quotes,
 * so a script that must mean the same under any login shell can have none of these four.
 */
function expectShellSafe(script: string): void {
  expect(script).not.toContain("'");
  expect(script).not.toContain('!');
  expect(script).not.toContain('\n');
  expect(script).not.toMatch(/\\\\/);
}

describe('remote commands', () => {
  it('carry any command, whatever it holds, intact and in a shell-safe script', () => {
    fc.assert(
      fc.property(fc.string({unit: 'binary', maxLength: 200}), text => {
        fc.pre(!text.includes('\0'));
        const line = remoteCommand(text);
        expect(commandOf(line)).toBe(text);
        const script = /^sh -c '([^']+)' /.exec(line)?.[1];
        expect(script).toBeDefined();
        expectShellSafe(script ?? '');
        // Nothing the command holds reaches the line outside the hex.
        expect(line.split(' ').at(-1)).toMatch(/^x[0-9a-f]*$/);
      }),
    );
  });

  it('refuse a command with a NUL, and read no line that is not one of theirs', () => {
    expect(() => remoteCommand('a\0b')).toThrow('NUL');
    expect(commandOf('ls -l')).toBeNull();
    expect(commandOf("sh -c 'x' vibesys-command xabc")).toBeNull();
  });
});
