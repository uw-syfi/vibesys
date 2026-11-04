import {expect, it} from '@effect/vitest';
import {CLIENT_TOO_OLD_TEXT, daemonTooOldText} from './protocol.js';

it('names the host executable in version refusals', () => {
  expect(CLIENT_TOO_OLD_TEXT).toBe(
    'this app is too old to talk to vibesys-host on this host; update the app',
  );
  expect(daemonTooOldText('0.1.0')).toBe(
    'vibesys-host on this host (0.1.0) is too old for this app; update vibesys-host there',
  );
});
