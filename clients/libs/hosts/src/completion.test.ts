import {describe, expect, test} from 'bun:test';
import {checkModel, type Seeded} from '@vibesys/store/testing';
import {
  COMPLETION_EMPTY,
  type CompletionAction,
  type CompletionResult,
  completionStep,
  type PathCompletion,
  splitPath,
  suggestions,
} from './index.js';

function failed(error: string): CompletionResult {
  return {ok: false, error};
}

const COMPLETION: PathCompletion = {
  kind: 'listed',
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

  test('unsupported completion leaves manual path entry usable without suggestions', () => {
    const asked = completionStep(COMPLETION_EMPTY, {
      type: 'input',
      field: 'project',
      host: 'ssh:old',
      value: '/manual/project',
    });
    const token = asked.requests[0]?.token ?? -1;
    const unsupported = completionStep(asked.state, {
      type: 'reply',
      field: 'project',
      token,
      result: {ok: true, value: {kind: 'unsupported'}},
    });

    expect(suggestions(unsupported.state, 'project')).toEqual([]);
    const next = completionStep(unsupported.state, {
      type: 'input',
      field: 'project',
      host: 'ssh:old',
      value: '/another/manual/path',
    });
    expect(next.requests).toHaveLength(1);
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
