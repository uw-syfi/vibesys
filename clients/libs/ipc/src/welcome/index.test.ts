import {describe, expect, test} from 'bun:test';
import {isInstanceId, isRunId, isTaskName} from '@vibesys/runs';
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
];

/** One valid argument vector for every privileged welcome request. */
const VALID_ARGUMENTS: Readonly<
  Record<(typeof WELCOME_CHANNELS)[keyof typeof WELCOME_CHANNELS], readonly unknown[]>
> = {
  [WELCOME_CHANNELS.overview]: [],
  [WELCOME_CHANNELS.hosts]: [],
  [WELCOME_CHANNELS.host]: ['local'],
  [WELCOME_CHANNELS.setCheckout]: ['local', '/checkout', 1],
  [WELCOME_CHANNELS.cancelCheckout]: [1],
  [WELCOME_CHANNELS.cancelInspection]: [1],
  [WELCOME_CHANNELS.signIn]: ['local'],
  [WELCOME_CHANNELS.complete]: ['local', '/project/'],
  [WELCOME_CHANNELS.inspect]: ['local', '0123456789ab', false, 1],
  [WELCOME_CHANNELS.tasks]: ['local', '/project'],
  [WELCOME_CHANNELS.start]: ['local', '/project', 'spsc', '', 1],
  [WELCOME_CHANNELS.cancelStart]: [1],
  [WELCOME_CHANNELS.attach]: ['local', '0123456789ab'],
  [WELCOME_CHANNELS.resume]: ['local', '/project', 'run-1'],
  [WELCOME_CHANNELS.showRun]: [],
  [WELCOME_CHANNELS.showWelcome]: [],
  [WELCOME_CHANNELS.retry]: [],
  [WELCOME_CHANNELS.stop]: ['local', '0123456789ab', false],
};

/** What every accepted request guarantees, whatever the view sent. */
function checkAccepted(request: WelcomeRequest): void {
  if ('instance' in request) expect(isInstanceId(request.instance)).toBe(true);
  if (request.kind === 'start') {
    expect(isTaskName(request.task)).toBe(true);
    expect(typeof request.args).toBe('string');
    expect(Number.isSafeInteger(request.start) && request.start > 0).toBe(true);
  }
  if (request.kind === 'set-checkout' || request.kind === 'cancel-checkout') {
    expect(Number.isSafeInteger(request.change) && request.change > 0).toBe(true);
  }
  if (request.kind === 'inspect' || request.kind === 'cancel-inspection') {
    expect(Number.isSafeInteger(request.inspection) && request.inspection > 0).toBe(true);
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

  test('a start carries the task, the run arguments as typed, and its token', () => {
    expect(
      parseWelcomeRequest(WELCOME_CHANNELS.start, ['local', '/p', 'spsc', '--rounds 2', 3]),
    ).toEqual({
      kind: 'start',
      host: {kind: 'local'},
      project: '/p',
      task: 'spsc',
      args: '--rounds 2',
      start: 3,
    });
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.start, ['local', '/p', 'spsc', ['--rounds'], 3]),
    ).toThrow('the run arguments must be text');
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

  test('inspection validates the host and id and treats only true as log consent', () => {
    expect(
      parseWelcomeRequest(WELCOME_CHANNELS.inspect, ['ssh:gpu', '0123456789ab', true, 7]),
    ).toEqual({
      kind: 'inspect',
      host: {kind: 'ssh', alias: 'gpu'},
      instance: '0123456789ab',
      includeLog: true,
      inspection: 7,
    });
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.inspect, ['local', '0123456789ab', 'true', 8]),
    ).toThrow('include log must be a boolean');
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.inspect, ['local', '0123456789ab', false, 0]),
    ).toThrow('an inspection is named by a positive integer');
    expect(() =>
      parseWelcomeRequest(WELCOME_CHANNELS.inspect, ['local', '../etc', false, 9]),
    ).toThrow('12-digit instance id');
    expect(parseWelcomeRequest(WELCOME_CHANNELS.cancelInspection, [9])).toEqual({
      kind: 'cancel-inspection',
      inspection: 9,
    });
    expect(() => parseWelcomeRequest(WELCOME_CHANNELS.cancelInspection, [0])).toThrow(
      'an inspection is named by a positive integer',
    );
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
        parseWelcomeRequest(WELCOME_CHANNELS.inspect, ['local', '0123456789ab', value, 1]),
      ).toMatchObject({kind: 'inspect', includeLog: value});
      expect(
        parseWelcomeRequest(WELCOME_CHANNELS.stop, ['local', '0123456789ab', value]),
      ).toMatchObject({kind: 'stop', force: value});
    }
  });

  test('regression: malformed boolean fields are rejected instead of authorizing false', () => {
    for (const value of [undefined, null, 0, 1, 'false', 'true', {}, []]) {
      expect(() =>
        parseWelcomeRequest(WELCOME_CHANNELS.inspect, ['local', '0123456789ab', value, 1]),
      ).toThrow('include log must be a boolean');
      expect(() =>
        parseWelcomeRequest(WELCOME_CHANNELS.stop, ['local', '0123456789ab', value]),
      ).toThrow('force stop must be a boolean');
    }
  });
});
