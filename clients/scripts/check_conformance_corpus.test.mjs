import assert from 'node:assert/strict';
import {mkdir, mkdtemp, writeFile} from 'node:fs/promises';
import {tmpdir} from 'node:os';
import {dirname, join, resolve} from 'node:path';
import test from 'node:test';
import {fileURLToPath} from 'node:url';
import {corpusErrors} from './check_conformance_corpus.mjs';

const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

const SCHEMA = {
  $defs: {
    EventType: {enum: ['run_started', 'run_finished']},
    RunEvent: {
      required: ['type', 'timestamp'],
      properties: {protocol_version: {}, type: {}, timestamp: {}, sequence: {}, run_id: {}},
    },
    SubscribeRequest: {properties: {type: {const: 'subscribe'}}},
    SubscribedMessage: {properties: {type: {const: 'subscribed'}}},
    EventBatchMessage: {properties: {type: {const: 'event_batch'}}},
  },
};

const CONTRACT = '### WP-ALPHA: first decision\n\ntext\n\n### WP-BETA: second decision\n\ntext\n';

function validEvent(type) {
  return {protocol_version: 1, type, sequence: 1, run_id: 'r', timestamp: '2026-01-01T00:00:00Z'};
}

function validScenario(id, decisions) {
  return {
    id,
    title: id,
    description: id,
    role: 'subscribe',
    transports: ['unix', 'websocket'],
    decisions,
    steps: [
      {dir: 'c2s', frame: {type: 'subscribe', after_sequence: 0}},
      {dir: 's2c', expect: {type: 'subscribed'}},
    ],
  };
}

function runnerInventory(id, scenarios) {
  return {id, description: `${id} runner`, groups: {steps: scenarios}};
}

async function writeTree(spec) {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-corpus-'));
  await mkdir(join(root, 'clients', 'backend-client', 'src', 'generated'), {recursive: true});
  await mkdir(join(root, 'docs', 'contributing'), {recursive: true});
  await mkdir(join(root, 'tests', 'conformance', 'events'), {recursive: true});
  await mkdir(join(root, 'tests', 'conformance', 'scenarios'), {recursive: true});
  await mkdir(join(root, 'tests', 'conformance', 'runners'), {recursive: true});
  await writeFile(
    join(root, 'clients', 'backend-client', 'src', 'generated', 'protocol.schema.json'),
    JSON.stringify(spec.schema ?? SCHEMA),
  );
  await writeFile(
    join(root, 'docs', 'contributing', 'wire-protocol.md'),
    spec.contract ?? CONTRACT,
  );
  for (const [name, data] of Object.entries(spec.events ?? {})) {
    await writeFile(join(root, 'tests', 'conformance', 'events', name), JSON.stringify(data));
  }
  for (const [name, data] of Object.entries(spec.scenarios ?? {})) {
    await writeFile(join(root, 'tests', 'conformance', 'scenarios', name), JSON.stringify(data));
  }
  const scenarioIds = Object.keys(spec.scenarios ?? {}).map(name => name.replace(/\.json$/, ''));
  const runners = spec.runners ?? {'server.json': runnerInventory('server', scenarioIds)};
  for (const [name, data] of Object.entries(runners)) {
    await writeFile(join(root, 'tests', 'conformance', 'runners', name), JSON.stringify(data));
  }
  return root;
}

function fullEvents() {
  return {
    'run_started.json': validEvent('run_started'),
    'run_finished.json': validEvent('run_finished'),
  };
}

test('the checked-in corpus is complete and well formed', async () => {
  assert.deepEqual(await corpusErrors(REPO_ROOT), []);
});

test('a well-formed temp corpus produces no errors', async () => {
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': validScenario('boot', ['WP-ALPHA', 'WP-BETA'])},
  });
  assert.deepEqual(await corpusErrors(root), []);
});

test('a multi-client scenario validates named transport actors', async () => {
  const scenario = validScenario('dual', ['WP-ALPHA', 'WP-BETA']);
  scenario.clients = {terminal: {transport: 'unix'}, browser: {transport: 'websocket'}};
  scenario.steps = scenario.steps.map((step, index) => ({
    ...step,
    client: index === 0 ? 'terminal' : 'browser',
  }));
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'dual.json': scenario},
  });
  assert.deepEqual(await corpusErrors(root), []);
});

test('a multi-client step cannot name an undeclared actor', async () => {
  const scenario = validScenario('dual', ['WP-ALPHA', 'WP-BETA']);
  scenario.clients = {terminal: {transport: 'unix'}, browser: {transport: 'websocket'}};
  scenario.steps = scenario.steps.map(step => ({...step, client: 'missing'}));
  const root = await writeTree({events: fullEvents(), scenarios: {'dual.json': scenario}});
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('must name a client declared by the scenario')));
});

test('a missing event fixture fails coverage', async () => {
  const root = await writeTree({
    events: {'run_started.json': validEvent('run_started')},
    scenarios: {'boot.json': validScenario('boot', ['WP-ALPHA', 'WP-BETA'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('EventType "run_finished" has no fixture')));
});

test('a malformed event fixture is rejected', async () => {
  const mismatched = validEvent('run_started');
  const unknownField = {...validEvent('run_finished'), bogus: 1};
  const root = await writeTree({
    events: {'run_started.json': mismatched, 'run_finished.json': unknownField},
    scenarios: {'boot.json': validScenario('boot', ['WP-ALPHA', 'WP-BETA'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('unknown RunEvent field "bogus"')));
});

test('a fixture missing a schema-required field is rejected', async () => {
  const {timestamp: _dropped, ...withoutTimestamp} = validEvent('run_finished');
  const root = await writeTree({
    events: {'run_started.json': validEvent('run_started'), 'run_finished.json': withoutTimestamp},
    scenarios: {'boot.json': validScenario('boot', ['WP-ALPHA', 'WP-BETA'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('missing required field "timestamp"')));
});

test('a scenario tagging an unknown decision is rejected', async () => {
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': validScenario('boot', ['WP-ALPHA', 'WP-BETA', 'WP-GHOST'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(
    errors.some(error => error.includes('decision "WP-GHOST" is not in the wire contract')),
  );
});

test('a contract decision no scenario exercises is reported', async () => {
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': validScenario('boot', ['WP-ALPHA'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('decision "WP-BETA" is exercised by no scenario')));
});

test('a malformed step is rejected', async () => {
  const scenario = validScenario('boot', ['WP-ALPHA', 'WP-BETA']);
  scenario.steps.push({dir: 's2c', expect: {type: 'not_a_real_frame'}});
  const root = await writeTree({events: fullEvents(), scenarios: {'boot.json': scenario}});
  const errors = await corpusErrors(root);
  assert.ok(
    errors.some(error => error.includes('unknown protocol message type "not_a_real_frame"')),
  );
});

test('a scenario id that disagrees with its filename is rejected', async () => {
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': validScenario('mismatch', ['WP-ALPHA', 'WP-BETA'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('"id" must equal the filename stem')));
});

test('every scenario is either runner-executed or declares its required setup, exclusively', async () => {
  const cases = [
    {
      executed: false,
      declaresSetup: false,
      expected: [
        'scenarios/boot.json: scenario must be runner-executed or declare "required_setup"',
      ],
    },
    {executed: false, declaresSetup: true, expected: []},
    {executed: true, declaresSetup: false, expected: []},
    {
      executed: true,
      declaresSetup: true,
      expected: ['scenarios/boot.json: runner-executed scenario must not declare "required_setup"'],
    },
  ];
  for (const {executed, declaresSetup, expected} of cases) {
    const scenario = validScenario('boot', ['WP-ALPHA', 'WP-BETA']);
    if (declaresSetup) scenario.required_setup = ['subscription-redial'];
    const root = await writeTree({
      events: fullEvents(),
      scenarios: {'boot.json': scenario},
      runners: executed ? {'server.json': runnerInventory('server', ['boot'])} : {},
    });
    const errors = await corpusErrors(root);
    const partitionErrors = errors.filter(
      error => error.includes('runner') || error.includes('required_setup'),
    );
    assert.deepEqual(
      partitionErrors,
      expected,
      `executed=${executed}, declaresSetup=${declaresSetup}`,
    );
  }
});

test('required setup names are non-empty capability tokens', async () => {
  const scenario = validScenario('boot', ['WP-ALPHA', 'WP-BETA']);
  scenario.required_setup = ['subscription-redial', 'not a token', 'subscription-redial'];
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': scenario},
    runners: {},
  });
  const errors = await corpusErrors(root);
  assert.ok(
    errors.some(error => error.includes('must contain unique kebab-case capability names')),
  );
});

test('a runner cannot register a scenario the corpus does not contain', async () => {
  const scenario = validScenario('boot', ['WP-ALPHA', 'WP-BETA']);
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': scenario},
    runners: {'server.json': runnerInventory('server', ['boot', 'missing'])},
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('unknown scenario "missing"')));
});

test('runner inventories reject empty metadata, groups, and duplicate registration', async () => {
  const scenario = validScenario('boot', ['WP-ALPHA', 'WP-BETA']);
  const root = await writeTree({
    events: fullEvents(),
    scenarios: {'boot.json': scenario},
    runners: {
      'server.json': {
        id: 'server',
        description: '',
        extra: true,
        groups: {first: ['boot'], second: ['boot'], 'bad group': []},
      },
    },
  });
  const errors = await corpusErrors(root);
  assert.ok(errors.some(error => error.includes('"description" must be a non-empty string')));
  assert.ok(errors.some(error => error.includes('unknown runner field "extra"')));
  assert.ok(errors.some(error => error.includes('registered in more than one group')));
  assert.ok(errors.some(error => error.includes('group name "bad group" must be kebab-case')));
  assert.ok(
    errors.some(error => error.includes('group "bad group" must be a non-empty scenario array')),
  );
});
