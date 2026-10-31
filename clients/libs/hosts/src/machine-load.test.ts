import {expect, test} from 'bun:test';
import fc from 'fast-check';
import type {ConnectionStatus} from './connection-status.js';
import {type HostListing, LISTING_STEPS} from './listing.js';
import {
  FAILURE_WORDS_MAX,
  failureWords,
  loadSteps,
  type MachineLoad,
  machineLoad,
} from './machine-load.js';

const STEP = fc.constantFrom(...LISTING_STEPS);

const CONNECTION: fc.Arbitrary<ConnectionStatus> = fc.oneof(
  fc.constant({kind: 'connecting'} as const),
  fc.constant({kind: 'connected'} as const),
  fc.integer({min: 1, max: 9}).map(attempt => ({kind: 'reconnecting', attempt}) as const),
  fc.string().map(detail => ({kind: 'offline', detail}) as const),
  fc.string().map(detail => ({kind: 'auth-needed', detail}) as const),
  fc.string().map(detail => ({kind: 'incompatible', detail}) as const),
  fc.constant({kind: 'run-ended'} as const),
  fc.constant({kind: 'closed'} as const),
  fc.string().map(detail => ({kind: 'capacity', detail}) as const),
);

test('property: every listing folds to one load whose step and words are named', () => {
  fc.assert(
    fc.property(CONNECTION, STEP, (connection, step) => {
      const listing: HostListing = {kind: 'unavailable', host: 'local', step, connection};
      const load = machineLoad('local', listing);
      expect(load.kind).not.toBe('listed');
      if (load.kind === 'listed') return;
      expect(load.step).toBe(step);
      if (load.kind === 'failed') expect(load.reason).not.toBe('');
      const steps = loadSteps(load);
      expect(steps.map(each => each.step)).toEqual([...LISTING_STEPS]);
      expect(steps.filter(each => each.state === 'pending' || each.state === 'done')).toHaveLength(
        LISTING_STEPS.length - 1,
      );
      expect(steps[LISTING_STEPS.indexOf(step)]?.state).toBe(
        load.kind === 'failed' ? 'failed' : 'current',
      );
    }),
  );
});

test('a machine that has not listed stands on the first step, one that has is listed', () => {
  expect(machineLoad('local', undefined)).toEqual({
    kind: 'loading',
    host: 'local',
    step: 'gateway',
    attempt: 0,
  });
  const listing: HostListing = {
    kind: 'listed',
    host: 'local',
    live: [],
    ended: [],
    unreadable: [],
    unverified: [],
  };
  expect(machineLoad('local', listing)).toEqual({kind: 'listed', host: 'local'});
});

test('steps read in order with the one under way marked and its position', () => {
  const load: MachineLoad = {kind: 'loading', host: 'local', step: 'handshake', attempt: 0};
  expect(loadSteps(load)).toEqual([
    {step: 'gateway', word: 'Starting gateway', state: 'done', position: '1/3'},
    {step: 'handshake', word: 'Connecting', state: 'current', position: '2/3'},
    {step: 'runs', word: 'Loading runs', state: 'pending', position: '3/3'},
  ]);
});

test('what a connection left behind reads in a few words', () => {
  for (const [detail, words] of [
    ['Error: gateway startup exited: 1', 'gateway exited (code 1)'],
    ['gateway connect exited: 255: host-a: no output from ssh', 'gateway exited (code 255)'],
    ['Error: gateway startup timed out', 'gateway timed out'],
    ['gateway did not connect in time', 'gateway timed out'],
    ['Run transport v2 requires gateway resources; install Go', 'gateway not built'],
    ['API hash mismatch: client a, gateway b', 'version mismatch'],
    [
      'ConnectionError: the connection to host-a is down: ssh: Could not resolve hostname host-a',
      'host not found',
    ],
    ['ConnectionError: the connection to host-a is down: Connection refused', 'connection down'],
    ['the run stream went silent', 'no answer'],
    ['the run stream ended', 'stream ended'],
    ['', 'no reason given'],
  ] as const) {
    expect(failureWords(detail)).toBe(words);
  }
});

test('property: failure words never carry a path', () => {
  const name = fc.stringMatching(/^[a-z0-9._-]{1,12}$/);
  const path = fc
    .tuple(fc.constantFrom('/', '~/', './', '../'), fc.array(name, {minLength: 1, maxLength: 6}))
    .map(([root, parts]) => `${root}${parts.join('/')}`);
  fc.assert(
    fc.property(
      path,
      fc.constantFrom(
        (each: string) => `Error: ENOTDIR: not a directory, mkdir '${each}'`,
        (each: string) => `EACCES: permission denied, open "${each}"`,
        (each: string) => `spawn ${each} failed`,
        (each: string) => `${each}: no such file`,
      ),
      (each, detail) => {
        expect(failureWords(detail(each))).not.toContain(each);
      },
    ),
  );
  expect(failureWords("Error: ENOTDIR: not a directory, mkdir '/a/b'")).toBe('not a directory');
  expect(failureWords('and/or either')).toBe('and/or either');
});

test('property: failure words are one short line, whatever the connection left behind', () => {
  fc.assert(
    fc.property(fc.string({maxLength: 400}), detail => {
      const words = failureWords(detail);
      expect(words.length).toBeGreaterThan(0);
      expect(words.length).toBeLessThanOrEqual(FAILURE_WORDS_MAX);
      expect(words).not.toContain('\n');
    }),
  );
});
