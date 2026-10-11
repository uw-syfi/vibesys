import {describe, test} from 'bun:test';
import {checkModel} from '@vibesys/core-state/testing';
import {exactResumeRunId} from '@vibesys/entities';
import type {WelcomeResult} from '@vibesys/ipc';
import {randomChrome} from '@vibesys/ipc/testing';
import {type RunCommand, WELCOME_INITIAL, type WelcomeAction, welcomeStep} from './run-control.js';

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

const COMMANDS: readonly RunCommand[] = [
  {kind: 'attach', host: 'local', instance: '0123456789ab'},
  {kind: 'resume', host: 'local', project: '/p', run: exactResumeRunId('run-1')},
  {kind: 'stop', host: 'local', instance: '0123456789ab', force: false},
  {kind: 'retry'},
];

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
    });
  });
});
