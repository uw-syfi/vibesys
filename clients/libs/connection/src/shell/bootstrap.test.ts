import {describe, expect, test} from 'bun:test';
import {localBootstrap, remoteBootstrap, remoteCommand} from './bootstrap.js';

/** `localBootstrap`'s script argument (index 1): the one piece every login shell re-parses unchanged. */
function scriptOf(checkout: string, uv: string | null): string {
  const script = localBootstrap(checkout, uv)[1];
  if (script === undefined) throw new Error('localBootstrap did not return a script argument');
  return script;
}

/**
 * The module doc comment's own invariant: csh and tcsh expand history `!` even inside single
 * quotes (unless it is immediately followed by whitespace), so a script that must mean the same
 * under any login shell can have none of these four.
 */
function expectShellSafe(script: string): void {
  expect(script).not.toContain("'");
  expect(script).not.toContain('!');
  expect(script).not.toContain('\n');
  expect(script).not.toMatch(/\\\\/);
}

/** A small seeded generator (fast-check is not a dependency; see the testing skill). */
function generator(seed: number): () => number {
  let state = seed >>> 0;
  return () => {
    state = (state * 1_664_525 + 1_013_904_223) >>> 0;
    return state / 2 ** 32;
  };
}

/** A path built from characters a login shell treats specially, to stress the hex-encoded data. */
function stressPath(random: () => number): string {
  const alphabet = [...`!'"\\$\`;&|<>(){}[] ab09~/.`];
  const length = Math.floor(random() * 12) + 1;
  return Array.from({length}, () => alphabet[Math.floor(random() * alphabet.length)]).join('');
}

describe('the bootstrap script a login shell re-parses before sh -c sees it', () => {
  test('contains no single quote, !, newline, or backslash pair', () => {
    expectShellSafe(scriptOf('/some/checkout', null));
  });

  test('is exactly what the ssh command line quotes and sends', () => {
    const script = scriptOf('/some/checkout', 'uv');
    expect(remoteBootstrap('/some/checkout', 'uv')).toContain(script);
  });

  test('stays the same, and stays shell-safe, for every checkout or uv path, however it is spelled', () => {
    const random = generator(7);
    const fixed = scriptOf('/some/checkout', null);
    for (let round = 0; round < 200; round += 1) {
      const checkout = stressPath(random);
      const uv = random() < 0.5 ? null : stressPath(random);
      const script = scriptOf(checkout, uv);
      expect(script).toBe(fixed);
      expectShellSafe(script);
    }
  });
});

test('gateway commands use the same login-shell-safe codec for arbitrary command text', () => {
  const random = generator(19);
  for (let round = 0; round < 200; round += 1) {
    const script = `${stressPath(random)}\n${stressPath(random)}`;
    const command = remoteCommand(script);
    const match = /^sh -c '([^']+)' vibesys-command x([a-f0-9]*)$/.exec(command);
    expect(match).not.toBeNull();
    expectShellSafe(match?.[1] ?? '');
    expect(Buffer.from(match?.[2] ?? '', 'hex').toString('utf8')).toBe(script);
  }
  expect(() => remoteCommand('x\0y')).toThrow('NUL');
});
