import {describe, expect, test} from 'bun:test';
import {QuitGate} from './quit-gate.js';

/** A quit whose host release the test settles by hand. */
function world() {
  const exits: number[] = [];
  let releases = 0;
  let settle: (failed: boolean) => void = () => {};
  const gate = new QuitGate({
    release: () => {
      releases += 1;
      return new Promise<void>((resolve, reject) => {
        settle = failed => (failed ? reject(new Error('ssh hung up')) : resolve());
      });
    },
    exit: code => exits.push(code),
  });
  let prevented = 0;
  const event = {preventDefault: () => (prevented += 1)};
  return {
    gate,
    event,
    exits,
    settle: (failed: boolean) => settle(failed),
    counts: () => ({releases, prevented}),
  };
}

describe('QuitGate', () => {
  test('regression: a quit ends the process once the hosts are released', async () => {
    // The app used to call app.quit() again from inside the cancelled quit, which Electron
    // ignores: the process stayed alive with no window.
    for (const failed of [false, true]) {
      const {gate, event, exits, settle, counts} = world();
      gate.willQuit(event);
      gate.willQuit(event);
      expect(counts()).toEqual({releases: 1, prevented: 2});
      expect(exits).toEqual([]);
      settle(failed);
      await gate.exit(0);
      expect(exits).toEqual([0]);
    }
  });

  test('a failed launch exits with its code after the release, once', async () => {
    const {gate, event, exits, settle, counts} = world();
    const done = gate.exit(1);
    gate.willQuit(event);
    settle(false);
    await done;
    expect(exits).toEqual([1]);
    expect(counts().releases).toBe(1);
  });
});
