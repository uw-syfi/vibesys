import {strict as assert} from 'node:assert';
import {test} from 'node:test';
import {errorText, type Fetch, HomeError, homeClient} from './home-api.js';

interface Call {
  url: string;
  method: string;
  headers: Record<string, string>;
  body: string | null;
}

/** A Fake `fetch`: records each call and answers with `reply`. */
function fakeFetch(reply: (url: string) => Response): {calls: Call[]; fetcher: Fetch} {
  const calls: Call[] = [];
  const fetcher: Fetch = async (url, init) => {
    calls.push({
      url,
      method: init.method ?? 'GET',
      headers: {...(init.headers as Record<string, string>)},
      body: typeof init.body === 'string' ? init.body : null,
    });
    return reply(url);
  };
  return {calls, fetcher};
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {status, headers: {'Content-Type': 'application/json'}});

test('every call carries the bearer token; bodies are JSON; path segments are encoded', async () => {
  const {calls, fetcher} = fakeFetch(() => json({tasks: []}));
  const client = homeClient('t0k', fetcher);
  await client.tasks('p/1');
  await client.validate('/Users/me/src/a b');
  await client.fs('/Users/me/a&b');
  await client.fs(null);
  await client.resume('p1', 'run 7', null);
  assert.deepEqual(
    calls.map(call => `${call.method} ${call.url}`),
    [
      'GET /api/projects/p%2F1/tasks',
      'POST /api/projects/validate',
      'GET /api/fs?path=%2FUsers%2Fme%2Fa%26b',
      'GET /api/fs',
      'POST /api/projects/p1/runs/run%207/resume',
    ],
  );
  assert.ok(calls.every(call => call.headers['Authorization'] === 'Bearer t0k'));
  assert.equal(calls[1]?.headers['Content-Type'], 'application/json');
  assert.equal(calls[1]?.body, '{"path":"/Users/me/src/a b"}');
  assert.equal(calls[4]?.body, '{"budget":null}');
  assert.equal(calls[0]?.body, null);
});

test('a typed error keeps its code, message and details', async () => {
  const {fetcher} = fakeFetch(() =>
    json(
      {
        error: {
          code: 'launch_failed',
          message: 'The run server exited with status 1',
          details: {stderr_tail: ['Traceback', 'ValueError: bad model'], stderr_log: '/x.log'},
        },
      },
      502,
    ),
  );
  const failure = await homeClient('t', fetcher)
    .start('p1', {
      task: 'decode',
      outer_loop: 'agent',
      budget: 12,
      compute_backend: 'metal',
      driver: null,
      provider: 'claude',
      model: 'claude-opus-5',
      reasoning_effort: null,
      roles: {},
    })
    .catch((error: unknown) => error);
  assert.ok(failure instanceof HomeError);
  assert.equal(failure.code, 'launch_failed');
  assert.deepEqual(failure.details?.['stderr_tail'], ['Traceback', 'ValueError: bad model']);
  assert.equal(
    errorText(failure),
    'The run server exited with status 1\nTraceback\nValueError: bad model',
  );
});

test('a key travels only in the PUT body; a rejection carries the server message only', async () => {
  const {calls, fetcher} = fakeFetch(() =>
    json({error: {code: 'invalid_key', message: 'The key contains a quote', details: null}}, 400),
  );
  const failure = await homeClient('t', fetcher)
    .saveKey('codex', 'OPENAI_API_KEY', 'sk-"secret')
    .catch((error: unknown) => error);
  assert.equal(calls.length, 1);
  assert.equal(calls[0]?.method, 'PUT');
  assert.equal(calls[0]?.url, '/api/auth/codex');
  assert.equal(calls[0]?.url.includes('secret'), false);
  assert.equal(calls[0]?.body, '{"name":"OPENAI_API_KEY","value":"sk-\\"secret"}');
  assert.ok(failure instanceof HomeError);
  assert.equal(errorText(failure).includes('secret'), false);
});

test('validation errors list their messages; unreachable and non-JSON answers become HomeErrors', async () => {
  const invalid = await homeClient(
    't',
    fakeFetch(() =>
      json(
        {
          error: {
            code: 'task_invalid',
            message: 'The task form is invalid',
            details: {errors: [{loc: ['objective'], msg: 'must not be empty'}]},
          },
        },
        422,
      ),
    ).fetcher,
  )
    .createTask('p1', {
      name: 'x',
      objective: '',
      domain: 'generic',
      accuracy_command: 'true',
      benchmark_command: 'true',
      result_json_argument: '--json',
      result_metric: 'm',
    })
    .catch((error: unknown) => error);
  assert.equal(errorText(invalid), 'The task form is invalid\nmust not be empty');
  const down = await homeClient('t', async () => {
    throw new TypeError('Failed to fetch');
  })
    .projects()
    .catch((error: unknown) => error);
  assert.ok(down instanceof HomeError);
  assert.equal(down.code, 'network');
  const html = await homeClient(
    't',
    fakeFetch(() => new Response('<h1>no</h1>', {status: 404})).fetcher,
  )
    .projects()
    .catch((error: unknown) => error);
  assert.ok(html instanceof HomeError);
  assert.equal(html.code, 'internal_error');
  assert.equal(html.message, 'The home server answered 404');
});
