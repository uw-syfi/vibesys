import {describe, expect, test} from 'bun:test';
import {isInstanceId, isRunId} from '@vibesys/runs';
import {Seeded} from '@vibesys/store/testing';
import {parseWelcomeRequest, WELCOME_CHANNELS, type WelcomeRequest} from '../index.js';

const VALUES: readonly unknown[] = [
  undefined,
  null,
  0,
  7,
  -1,
  1.5,
  true,
  '',
  'local',
  'ssh:gpu-box',
  'ssh:-oProxyCommand=x',
  'remote',
  '0123456789ab',
  'ZZZZ',
  'spsc',
  'Bad Task',
  '/srv/proj',
  'relative/proj',
  '-rf',
  'run-1',
  '--rounds 2',
  {},
  [],
  {provider: 'codex', model: 'gpt-a'},
  {provider: 'codex', model: 'gpt-a', extra: true},
  {provider: 'codex'},
  {provider: '', model: 'gpt-a'},
];

/** One valid argument vector for every privileged welcome request. */
const VALID_ARGUMENTS: Readonly<
  Record<(typeof WELCOME_CHANNELS)[keyof typeof WELCOME_CHANNELS], readonly unknown[]>
> = {
  [WELCOME_CHANNELS.overview]: [],
  [WELCOME_CHANNELS.setCheckout]: ['local', '/checkout', 1],
  [WELCOME_CHANNELS.cancelCheckout]: [1],
  [WELCOME_CHANNELS.signIn]: ['local'],
  [WELCOME_CHANNELS.complete]: ['local', '/project/'],
  [WELCOME_CHANNELS.logs]: ['local', '0123456789ab', {kind: 'list'}],
  [WELCOME_CHANNELS.releaseLogs]: [],
  [WELCOME_CHANNELS.tasks]: ['local', '/project'],
  [WELCOME_CHANNELS.harnesses]: ['local'],
  [WELCOME_CHANNELS.attach]: ['local', '0123456789ab'],
  [WELCOME_CHANNELS.resume]: ['local', '/project', 'run-1'],
  [WELCOME_CHANNELS.stopConfirmed]: ['local', '0123456789ab', false],
  [WELCOME_CHANNELS.releaseRun]: [],
  [WELCOME_CHANNELS.readEvents]: ['local', '/srv/project', 'run-1', 0, 100],
  [WELCOME_CHANNELS.preferences]: [],
  [WELCOME_CHANNELS.setAppearance]: [{mode: 'dark', light: 'mist', dark: 'midnight'}],
  [WELCOME_CHANNELS.setNotifications]: [false],
  [WELCOME_CHANNELS.archive]: [{host: 'local', runId: '20261015-163047'}, true],
  [WELCOME_CHANNELS.openRunWindow]: [{host: 'ssh:chelan4', runId: '20261015-163047'}],
  [WELCOME_CHANNELS.showMainWindow]: [],
  [WELCOME_CHANNELS.openExternal]: ['https://example.com/a'],
  [WELCOME_CHANNELS.machines]: [],
  [WELCOME_CHANNELS.machine]: ['ssh:chelan4'],
  [WELCOME_CHANNELS.inspectCheckout]: ['ssh:chelan4', '~/vibesys'],
  [WELCOME_CHANNELS.removeMachine]: ['ssh:chelan4'],
  [WELCOME_CHANNELS.describeMachine]: ['local'],
  [WELCOME_CHANNELS.preflight]: [
    'local',
    {project: '/p', task: 'mpmc', harness: null, options: {rounds: 2}, args: []},
  ],
  [WELCOME_CHANNELS.examples]: ['local'],
  [WELCOME_CHANNELS.helperFeatures]: ['ssh:chelan4'],
  [WELCOME_CHANNELS.createFromExample]: ['local', 'queue-rs', '~/projects/queue-rs'],
  [WELCOME_CHANNELS.copyOut]: ['local', '/p/inner', '/p-own'],
  [WELCOME_CHANNELS.startRun]: [
    'local',
    {
      project: '/p',
      task: 'mpmc',
      harness: {provider: 'claude', model: 'claude-a'},
      options: {},
      args: ['--x'],
    },
  ],
};

/** What every accepted request guarantees, whatever the view sent. */
function checkAccepted(request: WelcomeRequest): void {
  if ('instance' in request) expect(isInstanceId(request.instance)).toBe(true);
  if (request.kind === 'set-checkout' || request.kind === 'cancel-checkout') {
    expect(Number.isSafeInteger(request.change) && request.change > 0).toBe(true);
  }
  if (request.kind === 'resume') expect(isRunId(request.run)).toBe(true);
  if ('project' in request) expect(request.project.startsWith('/')).toBe(true);
  if ('host' in request) expect(['local', 'ssh']).toContain(request.host.kind);
}

describe('parseWelcomeRequest', () => {
  test('property: any arguments yield a validated request or an error naming the problem', () => {
    const random = new Seeded(21);
    const channels = Object.values(WELCOME_CHANNELS);
    for (let round = 0; round < 5_000; round += 1) {
      const channel = random.pick(channels);
      const args = Array.from({length: random.int(6)}, () => random.pick(VALUES));
      let request: WelcomeRequest;
      try {
        request = parseWelcomeRequest(channel, args);
      } catch (error) {
        expect(error).toBeInstanceOf(Error);
        expect((error as Error).message).not.toBe('');
        continue;
      }
      checkAccepted(request);
    }
  });

  test('a resume names the exact run, and a malformed run id is refused', () => {
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.resume, ['local', '/p', '-oProxyCommand']),
    ).toThrow('a run id is 1-128 lowercase');
  });

  test('checkout changes and cancellations carry the same validated operation token', () => {
    expect(parseWelcomeRequest(WELCOME_CHANNELS.setCheckout, ['local', '/new', 7])).toEqual({
      kind: 'set-checkout',
      host: {kind: 'local'},
      path: '/new',
      change: 7,
    });
    expect(parseWelcomeRequest(WELCOME_CHANNELS.cancelCheckout, [7])).toEqual({
      kind: 'cancel-checkout',
      change: 7,
    });
    expect(() => parseWelcomeRequest(WELCOME_CHANNELS.cancelCheckout, [0])).toThrow(
      'a checkout change is named by a positive integer',
    );
  });

  test('a logs request names its host and instance, and a read only that instance', () => {
    const read = {
      instance_id: '0123456789ab',
      stream_id: 's1',
      start: {tail_bytes: 1024},
      max_bytes: 4096,
      wait_ms: 0,
    };
    expect(
      parseWelcomeRequest(WELCOME_CHANNELS.logs, [
        'ssh:gpu',
        '0123456789ab',
        {kind: 'read', token: 'l1', read},
      ]),
    ).toEqual({
      kind: 'logs',
      host: {kind: 'ssh', alias: 'gpu'},
      instance: '0123456789ab',
      request: {kind: 'read', token: 'l1', read},
    });
    expect(
      parseWelcomeRequest(WELCOME_CHANNELS.logs, [
        'local',
        '0123456789ab',
        {kind: 'withdraw', token: 'l1'},
      ]),
    ).toMatchObject({kind: 'logs', request: {kind: 'withdraw', token: 'l1'}});
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.logs, [
        'local',
        '0123456789ab',
        {kind: 'read', token: 'l2', read: {...read, instance_id: 'ba9876543210'}},
      ]),
    ).toThrow('the logs read names ba9876543210, not 0123456789ab');
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.logs, ['local', '0123456789ab', {kind: 'tail'}]),
    ).toThrow('the logs request is not a listing, a read, or a withdrawal');
    expect(parseWelcomeRequest(WELCOME_CHANNELS.releaseLogs, [])).toEqual({kind: 'release-logs'});
  });

  test('an unknown channel is refused by name', () => {
    expect(() => parseWelcomeRequest('vibesys:welcome:format-disk', [])).toThrow(
      '"vibesys:welcome:format-disk" is not a welcome request',
    );
  });

  test('regression: every welcome request rejects missing or surplus arguments', () => {
    for (const channel of Object.values(WELCOME_CHANNELS)) {
      const args = VALID_ARGUMENTS[channel];
      expect(() => parseWelcomeRequest(channel, [...args, 'surplus'])).toThrow(
        `${channel} expects ${args.length} argument`,
      );
      if (args.length > 0) {
        expect(() => parseWelcomeRequest(channel, args.slice(0, -1))).toThrow(
          `${channel} expects ${args.length} argument`,
        );
      }
    }
  });

  test('required boolean fields preserve both true and false', () => {
    for (const value of [false, true]) {
      expect(
        parseWelcomeRequest(WELCOME_CHANNELS.stopConfirmed, ['local', '0123456789ab', value]),
      ).toMatchObject({kind: 'stop', force: value});
    }
  });

  test('regression: malformed boolean fields are rejected instead of authorizing false', () => {
    for (const value of [undefined, null, 0, 1, 'false', 'true', {}, []]) {
      expect(() =>
        parseWelcomeRequest(WELCOME_CHANNELS.stopConfirmed, ['local', '0123456789ab', value]),
      ).toThrow('force stop must be a boolean');
    }
  });
});
