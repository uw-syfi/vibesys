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

async function writeTree(spec) {
  const root = await mkdtemp(join(tmpdir(), 'vibesys-corpus-'));
  await mkdir(join(root, 'clients', 'backend-client', 'src', 'generated'), {recursive: true});
  await mkdir(join(root, 'docs', 'contributing'), {recursive: true});
  await mkdir(join(root, 'tests', 'conformance', 'events'), {recursive: true});
  await mkdir(join(root, 'tests', 'conformance', 'scenarios'), {recursive: true});
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
