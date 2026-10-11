import {describe, expect, test} from 'bun:test';
import {isInstanceId, isRunId, isTaskName} from '@vibesys/runs';
import {Seeded} from '@vibesys/store/testing';
import {parseWelcomeRequest, WELCOME_CHANNELS, type WelcomeRequest} from './index.js';

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

  test('an unknown channel is refused by name', () => {
    expect(() => parseWelcomeRequest('vibesys:welcome:format-disk', [])).toThrow(
      '"vibesys:welcome:format-disk" is not a welcome request',
    );
  });
});
