import {describe, expect, test} from 'bun:test';
import type {Seeded} from '@vibesys/core-state/testing';
import {checkModel} from '@vibesys/core-state/testing';
import {exactResumeRunId} from '../../resume-run.js';
import {randomChrome} from '../../testing/fake-chrome.js';
import type {WelcomeRecent, WelcomeRecentStatus, WelcomeResult} from '../../welcome-protocol.js';
import {
  PICKER_CLOSED,
  type PickerAction,
  type PickerState,
  pickerChoices,
  pickerInvariants,
  pickerStep,
} from './picker.js';
import {
  RECENT_EMPTY,
  type RecentAction,
  type RecentState,
  recentInvariants,
  recentStep,
  statusOf,
} from './recent.js';
import {type RunCommand, WELCOME_INITIAL, type WelcomeAction, welcomeStep} from './welcome.js';

function run(host: string, instanceId: string): WelcomeRecent {
  return {
    host,
    hostLabel: host,
    project: '/p',
    task: 'spsc',
    instanceId,
    runId: null,
    attachedAt: 1,
  };
}

const RUNS = [run('local', '0123456789ab'), run('ssh:gpu', 'ba9876543210')];

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

function randomRecentAction(random: Seeded, issued: readonly number[]): RecentAction {
  const status = random.pick<WelcomeRecentStatus>([
    {kind: 'live', instanceId: '0123456789ab', status: 'running'},
    {kind: 'ended'},
  ]);
  return random.pick<RecentAction>([
    {type: 'runs', runs: RUNS.filter(() => random.bool())},
    {type: 'chrome', chrome: randomChrome(random)},
    {type: 'tick'},
    {
      type: 'status-reply',
      host: random.pick(['local', 'ssh:gpu', 'ssh:other']),
      token: random.pick(issued),
      result: random.bool() ? {ok: true, value: {'0123456789ab': status}} : failed('offline'),
    },
  ]);
}

/** A refresh keeps showing the statuses known before it. */
function keepsKnown(before: RecentState, action: RecentAction, after: RecentState): void {
  if (action.type === 'status-reply') return;
  for (const listed of after.runs) {
    const was = before.hosts[listed.host];
    if (was?.error === null && was.known !== null) {
      expect(statusOf(after, listed)).toEqual(statusOf(before, listed));
    }
  }
}

describe('recent', () => {
  test('regression: a pushed list is shown and its statuses are asked for at once', () => {
    const next = recentStep(RECENT_EMPTY, {type: 'runs', runs: RUNS});
    expect(next.state.runs).toEqual(RUNS);
    expect(next.requests.map(request => request.host).sort()).toEqual(['local', 'ssh:gpu']);
  });

  test('model: one awaited status per host, stale replies dropped, known statuses kept', () => {
    checkModel({
      initial: RECENT_EMPTY,
      step: recentStep,
      action: randomRecentAction,
      issues: request => request.token,
      awaited: state =>
        Object.fromEntries(
          Object.entries(state.hosts).map(([host, statuses]) => [host, statuses.awaiting]),
        ),
      reply: action =>
        action.type === 'status-reply' ? {slot: action.host, token: action.token} : null,
      invariants: recentInvariants,
      also: (before, action, after, requests) => {
        const hosts = requests.map(request => request.host);
        expect(new Set(hosts).size).toBe(hosts.length);
        keepsKnown(before, action, after);
      },
    });
  });
});

function randomPickerAction(random: Seeded, issued: readonly number[]): PickerAction {
  return random.pick<PickerAction>([
    {type: 'open'},
    {type: 'close'},
    {type: 'query', value: random.pick(['', 'gpu', 'me@box', '-oProxy', 'zzz'])},
    {type: 'move', delta: random.pick([1, -1])},
    {type: 'choose'},
    {type: 'choose', index: random.int(4)},
    {
      type: 'aliases-reply',
      token: random.pick(issued),
      result: random.bool()
        ? {ok: true, value: ['gpu-box', 'gpu-2', 'login'].slice(0, random.int(4))}
        : failed('no config'),
    },
  ]);
}

describe('host picker', () => {
  test('model: the selection always names a listed row; choosing shows a listed host', () => {
    checkModel({
      initial: PICKER_CLOSED,
      step: pickerStep,
      action: randomPickerAction,
      issues: request => (request.kind === 'aliases' ? request.token : null),
      awaited: (state: PickerState) => ({
        aliases:
          state.kind === 'open' && state.aliases.kind === 'loading' ? state.aliases.token : null,
      }),
      reply: action =>
        action.type === 'aliases-reply' ? {slot: 'aliases', token: action.token} : null,
      invariants: pickerInvariants,
      also: (before, _action, after, requests) => {
        for (const request of requests) {
          if (request.kind !== 'show-host') continue;
          expect(after.kind).toBe('closed');
          const listed = pickerChoices(before).map(choice => `ssh:${choice.alias}`);
          expect(listed).toContain(request.host);
        }
      },
    });
  });
});

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
