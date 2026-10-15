import {describe, expect, test} from 'bun:test';
import type {HarnessListing, HarnessOffer, StartOutcome, WelcomeResult} from '@vibesys/ipc';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  awaited,
  chosenHarness,
  START_CLOSED,
  type StartAction,
  type StartRequest,
  type StartState,
  startable,
  startInvariants,
  startStep,
} from './start.js';

const HOST = {key: 'local', label: 'This Mac'};
const PROJECTS = ['/p/queue', '/p/kv'];
const TASKS = ['mpmc', 'mpsc', 'spsc', 'verus-mpmc-open'];
const HARNESSES: readonly HarnessOffer[] = [
  {provider: 'codex', model: 'gpt-a', default: true},
  {provider: 'codex', model: 'gpt-b', default: false},
  {provider: 'claude', model: 'claude-a', default: true},
];
const LISTED: WelcomeResult<HarnessListing> = {
  ok: true,
  value: {kind: 'listed', harnesses: HARNESSES},
};

function fail<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

/** A pair to pick: each listed one, the project default, and ones the host never offered. */
function randomChoice(random: Seeded): {provider: string; model: string} | null {
  return random.pick([
    null,
    ...HARNESSES.map(({provider, model}) => ({provider, model})),
    {provider: 'codex', model: 'claude-a'},
    {provider: 'gemini', model: 'free-text'},
  ]);
}

function randomStartAction(random: Seeded, issued: readonly number[]): StartAction {
  const token = random.pick(issued);
  return random.pick<StartAction>([
    {type: 'open', host: HOST, projects: random.bool() ? PROJECTS : []},
    {type: 'close'},
    {type: 'project', value: random.pick(['', '/p/queue', '/p/kv', ' /p/kv '])},
    {type: 'args', value: random.pick(['', '--rounds 2'])},
    {type: 'list-tasks'},
    {type: 'pick', task: random.pick([...TASKS, 'unknown'])},
    {type: 'harness', value: randomChoice(random)},
    {type: 'start'},
    {type: 'run-shown'},
    {
      type: 'tasks-reply',
      token,
      result: random.bool()
        ? {ok: true, value: TASKS.slice(0, random.int(TASKS.length + 1))}
        : fail('no project'),
    },
    {
      type: 'harnesses-reply',
      token,
      result: random.pick<WelcomeResult<HarnessListing>>([
        LISTED,
        {ok: true, value: {kind: 'listed', harnesses: HARNESSES.slice(0, random.int(4))}},
        {ok: true, value: {kind: 'unsupported'}},
        fail('the host is unreachable'),
      ]),
    },
    {
      type: 'start-reply',
      token,
      result: random.pick<WelcomeResult<StartOutcome>>([
        {ok: true, value: 'started'},
        {ok: true, value: 'cancelled'},
        fail('boom'),
      ]),
    },
  ]);
}

/** Fold `actions` from `state`, returning the end state and every request emitted. */
function fold(state: StartState, actions: readonly StartAction[]) {
  const requests: StartRequest[] = [];
  for (const action of actions) {
    const next = startStep(state, action);
    state = next.state;
    requests.push(...next.requests);
  }
  return {state, requests};
}

/**
 * A start action asks to start a run exactly when the sheet could start one, with the pair the
 * sheet shows as chosen (null: the project default), never another.
 */
function expectStartRequest(before: StartState, requests: readonly StartRequest[]): void {
  const started = requests.flatMap(request =>
    request.kind === 'start' ? [{task: request.task, harness: request.harness}] : [],
  );
  const task = startable(before);
  expect(started).toEqual(task === null ? [] : [{task, harness: chosenHarness(before)}]);
}

function tokenOf(requests: readonly StartRequest[], kind: StartRequest['kind']): number {
  const found = requests.filter(request => request.kind === kind).at(-1);
  if (found === undefined) throw new Error(`no ${kind} request`);
  return found.token;
}

describe('start sheet', () => {
  test('regression: a slow earlier task list never replaces the list the user picked from', () => {
    const opened = fold(START_CLOSED, [
      {type: 'open', host: HOST, projects: PROJECTS},
      {type: 'list-tasks'},
    ]);
    const [first, second] = opened.requests.filter(request => request.kind === 'tasks');
    if (first === undefined || second === undefined) throw new Error('two lists expected');
    const picked = fold(opened.state, [
      {type: 'tasks-reply', token: second.token, result: {ok: true, value: TASKS}},
      {type: 'pick', task: 'mpsc'},
    ]).state;
    const late = startStep(picked, {
      type: 'tasks-reply',
      token: first.token,
      result: {ok: true, value: ['other']},
    });
    expect(late.state).toBe(picked);
    expect(startable(late.state)).toBe('mpsc');
  });

  test('regression: editing the project drops its tasks, so Start is off until they are listed again', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    const listed = fold(opened.state, [
      {
        type: 'tasks-reply',
        token: tokenOf(opened.requests, 'tasks'),
        result: {ok: true, value: TASKS},
      },
      {type: 'pick', task: 'spsc'},
    ]).state;
    expect(startable(listed)).toBe('spsc');
    const edited = startStep(listed, {type: 'project', value: '/p/kv'}).state;
    expect(startable(edited)).toBeNull();
    expect(startStep(edited, {type: 'start'}).requests).toEqual([]);
  });

  test('regression: Cancel while starting cancels the start and closes only when it answers', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    const started = fold(opened.state, [
      {
        type: 'tasks-reply',
        token: tokenOf(opened.requests, 'tasks'),
        result: {ok: true, value: ['spsc']},
      },
      {type: 'start'},
    ]);
    expect(started.state.kind).toBe('starting');
    const token = tokenOf(started.requests, 'start');
    const cancelled = startStep(started.state, {type: 'close'});
    expect(cancelled.state.kind).toBe('cancelling');
    expect(cancelled.requests).toEqual([{kind: 'cancel-start', token}]);
    // Esc again changes nothing; the start's own reply ends the sheet's work.
    expect(startStep(cancelled.state, {type: 'close'}).state).toBe(cancelled.state);
    for (const result of [
      {ok: true, value: 'cancelled'},
      {ok: true, value: 'started'},
      fail<StartOutcome>('docker is not running'),
    ] as const) {
      expect(startStep(cancelled.state, {type: 'start-reply', token, result}).state.kind).toBe(
        'closed',
      );
    }
  });

  test('a failed start keeps the form and its list, with the reason', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    const started = fold(opened.state, [
      {
        type: 'tasks-reply',
        token: tokenOf(opened.requests, 'tasks'),
        result: {ok: true, value: TASKS},
      },
      {type: 'pick', task: 'mpmc'},
      {type: 'start'},
    ]);
    const failed = startStep(started.state, {
      type: 'start-reply',
      token: tokenOf(started.requests, 'start'),
      result: fail('the image build failed'),
    }).state;
    expect(failed.kind).toBe('editing');
    expect(startable(failed)).toBe('mpmc');
    if (failed.kind === 'editing') expect(failed.error).toBe('the image build failed');
  });

  test('opening asks the host for its vetted models, and the picked one reaches the start', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    expect(opened.requests.map(request => request.kind)).toEqual(['harnesses', 'tasks']);
    const picked = fold(opened.state, [
      {type: 'harnesses-reply', token: tokenOf(opened.requests, 'harnesses'), result: LISTED},
      {
        type: 'tasks-reply',
        token: tokenOf(opened.requests, 'tasks'),
        result: {ok: true, value: ['spsc']},
      },
      {type: 'harness', value: {provider: 'claude', model: 'claude-a'}},
      {type: 'start'},
    ]);
    const [start] = picked.requests.filter(request => request.kind === 'start');
    expect(start).toMatchObject({task: 'spsc', harness: {provider: 'claude', model: 'claude-a'}});
  });

  test('the project default, a host without the choice, and an unlisted pair all start with none', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    const harnesses = tokenOf(opened.requests, 'harnesses');
    const tasks = {
      type: 'tasks-reply',
      token: tokenOf(opened.requests, 'tasks'),
      result: {ok: true, value: ['spsc']},
    } as const;
    const cases: readonly (readonly StartAction[])[] = [
      [
        {type: 'harnesses-reply', token: harnesses, result: LISTED},
        {type: 'harness', value: {provider: 'codex', model: 'gpt-b'}},
        {type: 'harness', value: null},
      ],
      [
        {
          type: 'harnesses-reply',
          token: harnesses,
          result: {ok: true, value: {kind: 'unsupported'}},
        },
      ],
      [
        {type: 'harnesses-reply', token: harnesses, result: LISTED},
        {type: 'harness', value: {provider: 'codex', model: 'typed-by-hand'}},
      ],
      [],
    ];
    for (const actions of cases) {
      const started = fold(opened.state, [tasks, ...actions, {type: 'start'}]);
      const [start] = started.requests.filter(request => request.kind === 'start');
      expect(start).toMatchObject({task: 'spsc', harness: null});
    }
  });

  test('regression: a model list from an earlier opening never fills a later sheet', () => {
    const first = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    const stale = tokenOf(first.requests, 'harnesses');
    const reopened = fold(first.state, [
      {type: 'close'},
      {type: 'open', host: {key: 'ssh:gpu', label: 'gpu'}, projects: PROJECTS},
    ]);
    const late = startStep(reopened.state, {type: 'harnesses-reply', token: stale, result: LISTED});
    expect(late.state).toBe(reopened.state);
    expect(awaited(late.state).harnesses).toBe(tokenOf(reopened.requests, 'harnesses'));
  });

  test('a failed start shows the form again with the model that was picked', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST, projects: PROJECTS}]);
    const started = fold(opened.state, [
      {type: 'harnesses-reply', token: tokenOf(opened.requests, 'harnesses'), result: LISTED},
      {
        type: 'tasks-reply',
        token: tokenOf(opened.requests, 'tasks'),
        result: {ok: true, value: ['spsc']},
      },
      {type: 'harness', value: {provider: 'codex', model: 'gpt-b'}},
      {type: 'start'},
    ]);
    const failed = startStep(started.state, {
      type: 'start-reply',
      token: tokenOf(started.requests, 'start'),
      result: fail('model gpt-b is not vetted'),
    }).state;
    expect(failed.kind).toBe('editing');
    expect(chosenHarness(failed)).toEqual({provider: 'codex', model: 'gpt-b'});
  });

  test('model: invariants hold and stale replies are dropped over random sequences', () => {
    checkModel({
      initial: START_CLOSED,
      step: startStep,
      action: randomStartAction,
      issues: request => (request.kind === 'cancel-start' ? null : request.token),
      awaited,
      reply: action => {
        if (action.type === 'tasks-reply') return {slot: 'tasks', token: action.token};
        if (action.type === 'harnesses-reply') return {slot: 'harnesses', token: action.token};
        if (action.type === 'start-reply') return {slot: 'start', token: action.token};
        return null;
      },
      invariants: startInvariants,
      // `startInvariants` holds a chosen pair to the host's list.
      also: (before, action, after, requests) => {
        if (action.type === 'start') expectStartRequest(before, requests);
        // Start is enabled only for a picked task listed for the project shown.
        const task = startable(after);
        if (task === null) return;
        expect(after.kind === 'editing' && after.tasks.kind === 'listed').toBe(true);
        if (after.kind === 'editing' && after.tasks.kind === 'listed') {
          expect(after.tasks.for).toBe(after.project.trim());
          expect(after.tasks.list).toContain(task);
        }
      },
    });
  });
});
