import {describe, expect, test} from 'bun:test';
import {checkModel} from '../../testing/model.js';
import type {Seeded} from '../../testing/seeded.js';
import type {StartOutcome, WelcomeHost, WelcomeResult} from '../../welcome-protocol.js';
import {
  awaited,
  START_CLOSED,
  type StartAction,
  type StartRequest,
  type StartState,
  startable,
  startInvariants,
  startStep,
} from './start.js';

const HOST: WelcomeHost = {
  key: 'local',
  label: 'This Mac',
  checkout: '/src/vibesys',
  suggestedCheckout: null,
  runs: [],
  projects: ['/p/queue', '/p/kv'],
};
const TASKS = ['mpmc', 'mpsc', 'spsc', 'verus-mpmc-open'];

function fail<T>(error: string): WelcomeResult<T> {
  return {ok: false, error, authNeeded: false};
}

function randomStartAction(random: Seeded, issued: readonly number[]): StartAction {
  const token = random.pick(issued);
  return random.pick<StartAction>([
    {type: 'open', host: {...HOST, projects: random.bool() ? HOST.projects : []}},
    {type: 'close'},
    {type: 'project', value: random.pick(['', '/p/queue', '/p/kv', ' /p/kv '])},
    {type: 'args', value: random.pick(['', '--rounds 2'])},
    {type: 'list-tasks'},
    {type: 'pick', task: random.pick([...TASKS, 'unknown'])},
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

function tokenOf(requests: readonly StartRequest[], kind: StartRequest['kind']): number {
  const found = requests.filter(request => request.kind === kind).at(-1);
  if (found === undefined) throw new Error(`no ${kind} request`);
  return found.token;
}

describe('start sheet', () => {
  test('regression: a slow earlier task list never replaces the list the user picked from', () => {
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST}, {type: 'list-tasks'}]);
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
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST}]);
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
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST}]);
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
    const opened = fold(START_CLOSED, [{type: 'open', host: HOST}]);
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

  test('model: invariants hold and stale replies are dropped over random sequences', () => {
    checkModel({
      initial: START_CLOSED,
      step: startStep,
      action: randomStartAction,
      issues: request => (request.kind === 'cancel-start' ? null : request.token),
      reply: (state, action) => {
        if (action.type === 'tasks-reply')
          return {token: action.token, awaited: awaited(state).tasks};
        if (action.type === 'start-reply')
          return {token: action.token, awaited: awaited(state).start};
        return null;
      },
      invariants: startInvariants,
      also: (_before, _action, after) => {
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
