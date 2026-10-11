import {describe, expect, test} from 'bun:test';
import type {WelcomeResult} from '@vibesys/ipc';
import {randomChrome} from '@vibesys/ipc/testing';
import {exactResumeRunId} from '@vibesys/runs';
import {checkModel} from '@vibesys/store/testing';
import {
  type RunCommand,
  WELCOME_INITIAL,
  type WelcomeAction,
  type WelcomeState,
  welcomeStep,
} from './run-control.js';

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

const COMMANDS: readonly RunCommand[] = [
  {kind: 'attach', host: 'local', instance: '0123456789ab'},
  {kind: 'resume', host: 'local', project: '/p', run: exactResumeRunId('run-1')},
  {kind: 'stop', host: 'local', instance: '0123456789ab', force: false},
  {kind: 'retry'},
  {kind: 'show-run'},
  {kind: 'show-welcome'},
];

/**
 * The kinds `progress()` has no text for. They must retire superseded progress, but may retain an
 * asynchronous problem that is not owned by the prior command.
 */
const TEXTLESS_KINDS: ReadonlySet<RunCommand['kind']> = new Set([
  'stop',
  'retry',
  'show-run',
  'show-welcome',
]);

function assertTextlessCommandBanner(
  before: WelcomeState,
  command: RunCommand,
  after: WelcomeState,
): void {
  if (!TEXTLESS_KINDS.has(command.kind)) return;
  // A new token invalidates a previous attach/resume's progress, but not a problem event that
  // arrived independently of a command.
  if (before.banner?.source === 'progress') expect(after.banner).toBeNull();
  if (before.banner?.source === 'problem') expect(after.banner).toEqual(before.banner);
}

function assertProblemReplyBanner(
  before: WelcomeState,
  action: Extract<WelcomeAction, {type: 'reply'}>,
  after: WelcomeState,
): void {
  if (!action.result.ok || before.awaiting !== action.token) return;
  if (before.banner?.source === 'problem') expect(after.banner).toEqual(before.banner);
}

function assertBannerInvariant(
  before: WelcomeState,
  action: WelcomeAction,
  after: WelcomeState,
): void {
  if (action.type === 'command') {
    assertTextlessCommandBanner(before, action.command, after);
    return;
  }
  if (action.type === 'reply') assertProblemReplyBanner(before, action, after);
}

describe('welcome banner', () => {
  test('model: the banner answers the latest action only', () => {
    checkModel({
      initial: WELCOME_INITIAL,
      step: welcomeStep,
      action: (random, issued): WelcomeAction =>
        random.pick<WelcomeAction>([
          {type: 'command', command: random.pick(COMMANDS)},
          {type: 'chrome', chrome: randomChrome(random)},
          {
            type: 'reply',
            token: random.pick(issued),
            result: random.bool() ? {ok: true, value: null} : failed('failed'),
          },
        ]),
      issues: request => request.token,
      awaited: state => ({banner: state.awaiting}),
      reply: action => (action.type === 'reply' ? {slot: 'banner', token: action.token} : null),
      also: assertBannerInvariant,
    });
  });

  test('regression: a textless stop command clears the progress text of an outstanding attach', () => {
    const attaching = welcomeStep(WELCOME_INITIAL, {
      type: 'command',
      command: {kind: 'attach', host: 'local', instance: 'run-a'},
    });
    expect(attaching.state.banner).toEqual({
      text: 'Attaching to run-a…',
      error: false,
      source: 'progress',
    });

    // Before the attach reply lands, the user stops a different run. `stop` has no progress text
    // of its own, but it still supersedes the attach: `awaiting` moves on, and the banner must not
    // keep describing the now-abandoned attach.
    const stopping = welcomeStep(attaching.state, {
      type: 'command',
      command: {kind: 'stop', host: 'local', instance: 'run-b', force: false},
    });
    expect(stopping.state.awaiting).not.toBe(attaching.state.awaiting);
    expect(stopping.state.banner).toBeNull();
  });

  test('regression: a textless command and its successful reply retain an asynchronous problem', () => {
    const problem = welcomeStep(WELCOME_INITIAL, {type: 'problem', text: 'Connection lost.'});
    const stopping = welcomeStep(problem.state, {
      type: 'command',
      command: {kind: 'stop', host: 'local', instance: 'run-a', force: false},
    });
    const [request] = stopping.requests;

    expect(stopping.state.banner).toEqual({
      text: 'Connection lost.',
      error: true,
      source: 'problem',
    });
    expect(request).toBeDefined();
    if (request === undefined) throw new Error('the stop command should be requested');

    const stopped = welcomeStep(stopping.state, {
      type: 'reply',
      token: request.token,
      result: {ok: true, value: null},
    });
    expect(stopped.state.awaiting).toBeNull();
    expect(stopped.state.banner).toEqual(stopping.state.banner);
  });
});
