import {expect, it} from '@effect/vitest';
import {CLIENT_BUILD, CLIENT_TOO_OLD_TEXT, daemonTooOldText} from './protocol.js';

/** The first host protocol that serves `WatchRuns` detail SUMMARY, its summary frames and `GetRunHistory`. */
const FIRST_HOST_WITH_SUMMARIES = 2;

it('names the host executable in version refusals', () => {
  expect(CLIENT_TOO_OLD_TEXT).toBe(
    'this app is too old to talk to vibesys-host on this host; update the app',
  );
  expect(daemonTooOldText('0.1.0')).toBe(
    'vibesys-host on this host (0.1.0) is too old for this app; update vibesys-host there',
  );
});

it('asks for a host that serves summaries, so an older host answers DAEMON_TOO_OLD', () => {
  // A host judges from the client's minimum: it is too old when that is above its own protocol.
  expect(CLIENT_BUILD.minProtocolVersion).toBeGreaterThanOrEqual(FIRST_HOST_WITH_SUMMARIES);
  expect(CLIENT_BUILD.protocolVersion).toBeGreaterThanOrEqual(CLIENT_BUILD.minProtocolVersion);
});
