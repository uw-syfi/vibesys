import {describe, expect, test} from 'bun:test';
import {checkModel, type Seeded} from '@vibesys/core-state/testing';
import {exactResumeRunId, type HostListing, type RecentRun} from '@vibesys/entities';
import type {PathCompletion, WelcomeResult} from '@vibesys/ipc';
import {randomChrome} from '../../../testing/fake-chrome.js';
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  completionStep,
  splitPath,
  suggestions,
} from './completion.js';
import {LISTS_EMPTY, type ListsAction, listsInvariants, listsStep, statusOf} from './lists.js';
import {
  PICKER_CLOSED,
  type PickerAction,
  type PickerState,
  pickerChoices,
  pickerInvariants,
  pickerStep,
} from './picker.js';
import {type RunCommand, WELCOME_INITIAL, type WelcomeAction, welcomeStep} from './welcome.js';

function run(host: string, runId: string): RecentRun {
  return {host, hostLabel: host, project: '/p', task: 'spsc', runId, attachedAt: 1};
}

const RUNS = [run('local', 'run-1'), run('ssh:gpu', 'run-2')];

function failed<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

function listing(host: string, live: string[], ended: string[]): HostListing {
  const entry = (runId: string, index: number) => ({
    id: `00000000000${index}`,
    projectRoot: '/p',
    runId,
    status: 'running',
    startedAt: 1,
    reason: null,
  });
  return {kind: 'listed', host, live: live.map(entry), ended: ended.map(entry), unreadable: []};
}

function randomListsAction(random: Seeded): ListsAction {
  const host = random.pick(['local', 'ssh:gpu']);
  return random.pick<ListsAction>([
    {type: 'runs', runs: RUNS.filter(() => random.bool())},
    {
      type: 'listing',
      listing: listing(host, random.bool() ? ['run-1'] : [], random.bool() ? ['run-2'] : []),
    },
    {
      type: 'listing',
      listing: {kind: 'unavailable', host, connection: {kind: 'reconnecting', attempt: 1}},
    },
  ]);
}

describe('lists', () => {
  test('model: listings fold from pushes only, and a run is live exactly when its host lists it', () => {
    checkModel({
      initial: LISTS_EMPTY,
      step: listsStep,
      action: random => randomListsAction(random),
      issues: () => null,
      awaited: () => ({}),
      reply: () => null,
      invariants: listsInvariants,
      also: (_before, _action, after, requests) => {
        expect(requests).toEqual([]);
        for (const recent of after.runs) {
          const shown = after.listings[recent.host];
          const live =
            shown?.kind === 'listed' && shown.live.some(entry => entry.runId === recent.runId);
          expect(statusOf(after, recent).kind === 'live').toBe(live);
        }
      },
    });
  });
});

const COMPLETION: PathCompletion = {
  directory: '/home/me',
  entries: [
    {name: 'src', path: '/home/me/src', checkout: false, project: false},
    {name: 'scratch', path: '/home/me/scratch', checkout: false, project: true},
    {name: 'vibesys', path: '/home/me/vibesys', checkout: true, project: false},
  ],
  truncated: false,
};

function randomCompletionAction(random: Seeded, issued: readonly number[]): CompletionAction {
  const field = random.pick(['checkout', 'project'] as const);
  return random.pick<CompletionAction>([
    {
      type: 'input',
      field,
      host: random.pick(['local', 'ssh:gpu']),
      value: random.pick(['', '/home/me/', '/home/me/s', '/home/me/sc', '~/', 'rel/x', '/']),
    },
    {
      type: 'reply',
      field,
      token: random.pick(issued),
      result: random.bool() ? {ok: true, value: COMPLETION} : failed('path_missing'),
    },
  ]);
}

describe('path completion', () => {
  test('regression: typing within a directory filters its one listing without asking again', () => {
    const asked = completionStep(COMPLETION_EMPTY, {
      type: 'input',
      field: 'project',
      host: 'local',
      value: '/home/me/',
    });
    expect(asked.requests).toHaveLength(1);
    const token = asked.requests[0]?.token ?? -1;
    const listed = completionStep(asked.state, {
      type: 'reply',
      field: 'project',
      token,
      result: {ok: true, value: COMPLETION},
    });
    const typed = completionStep(listed.state, {
      type: 'input',
      field: 'project',
      host: 'local',
      value: '/home/me/sc',
    });
    expect(typed.requests).toEqual([]);
    expect(suggestions(typed.state, 'project')).toEqual(['/home/me/scratch/']);
  });

  test('a path names its directory up to the last slash, only when absolute or under ~/', () => {
    expect(splitPath('/home/me/sr')).toEqual({directory: '/home/me/', typed: 'sr'});
    expect(splitPath('~/')).toEqual({directory: '~/', typed: ''});
    expect(splitPath('rel/x')).toEqual({directory: null, typed: 'x'});
  });

  test('model: one awaited listing per field, stale replies dropped, one request per new directory', () => {
    checkModel({
      initial: COMPLETION_EMPTY,
      step: completionStep,
      action: randomCompletionAction,
      issues: request => request.token,
      awaited: state => ({
        checkout: state.fields.checkout?.awaiting ?? null,
        project: state.fields.project?.awaiting ?? null,
      }),
      reply: action => (action.type === 'reply' ? {slot: action.field, token: action.token} : null),
      also: (before, action, _after, requests) => {
        if (action.type !== 'input') return;
        const was = before.fields[action.field];
        const {directory} = splitPath(action.value);
        const same = was?.host === action.host && was.directory === directory;
        expect(requests.length).toBe(same || directory === null ? 0 : 1);
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
