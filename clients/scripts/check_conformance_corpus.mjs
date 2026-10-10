#!/usr/bin/env node

// Validates the shared transport conformance corpus at `<repo>/tests/conformance` against the
// generated protocol schema and the wire contract. Dependency-free on purpose: the workspace pins
// only biome, dependency-cruiser, and knip, so this check hand-rolls the structural validation it
// needs rather than pulling in a JSON-schema library. Message shape is not hand-encoded here: the
// event envelope (its fields and which are required) is read from the generated schema, the single
// source protocol codegen owns (see #850). See tests/conformance/README.md and FORMAT.md.

import {readdir, readFile} from 'node:fs/promises';
import {basename, dirname, join, resolve} from 'node:path';
import {fileURLToPath} from 'node:url';

const SCHEMA_RELATIVE = 'backend-client/src/generated/protocol.schema.json';
const CONTRACT_RELATIVE = 'docs/contributing/wire-protocol.md';
const CORPUS_RELATIVE = 'tests/conformance';
const RUNNERS_RELATIVE = 'runners';

const STEP_DIRECTIONS = new Set(['c2s', 's2c']);
const SCENARIO_ROLES = new Set(['control', 'subscribe', 'chat']);
const KNOWN_TRANSPORTS = new Set(['unix', 'websocket']);
const RUNNER_FIELDS = new Set(['id', 'description', 'groups']);
const SETUP_CAPABILITY = /^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$/;
// A control-path reply is a `Response`, which has no `type` discriminant, so scenarios name it with
// this pseudo-type. Every other frame type is derived from the schema. The name is not defined
// here: it is the section the generated schema publishes `Response` under, derived in
// `tests/conformance/frame_matching.py`. This gate runs in its own process and cannot import that,
// so it restates the literal and `test_frame_matching.py` fails if the two ever disagree.
const RESPONSE_PSEUDO_TYPE = 'response';

function typeConsts(schema) {
  const consts = new Set();
  for (const def of Object.values(schema.$defs ?? {})) {
    const value = def?.properties?.type?.const;
    if (typeof value === 'string') consts.add(value);
  }
  return consts;
}

/** The `EventType` enum: the authoritative event-kind set the corpus must cover one-for-one. */
function eventKinds(schema) {
  return new Set(schema.$defs?.EventType?.enum ?? []);
}

/** The `RunEvent` envelope: which top-level fields an event fixture may carry. */
function runEventFields(schema) {
  return new Set(Object.keys(schema.$defs?.RunEvent?.properties ?? {}));
}

/** The `RunEvent` required fields, read from the schema rather than hand-encoded. */
function runEventRequiredFields(schema) {
  return schema.$defs?.RunEvent?.required ?? [];
}

/** Valid frame types a scenario may name: every discriminant const that is not an event kind. */
function frameTypes(schema) {
  const kinds = eventKinds(schema);
  const frames = new Set([RESPONSE_PSEUDO_TYPE]);
  for (const value of typeConsts(schema)) {
    if (!kinds.has(value)) frames.add(value);
  }
  return frames;
}

/** Decision tokens declared as `### WP-...:` headings in the wire contract. */
function contractDecisions(markdown) {
  const decisions = new Set();
  const pattern = /^###\s+(WP-[A-Z-]+):/gm;
  let match = pattern.exec(markdown);
  while (match !== null) {
    decisions.add(match[1]);
    match = pattern.exec(markdown);
  }
  return decisions;
}

async function readJsonDir(dir) {
  let entries;
  try {
    entries = await readdir(dir);
  } catch (error) {
    return {files: [], error: `cannot read ${dir}: ${error.message}`};
  }
  const files = [];
  for (const name of entries.filter(entry => entry.endsWith('.json')).sort()) {
    const stem = basename(name, '.json');
    try {
      files.push({name, stem, data: JSON.parse(await readFile(join(dir, name), 'utf8'))});
    } catch (error) {
      files.push({name, stem, parseError: error.message});
    }
  }
  return {files, error: null};
}

function checkEventFixture(fixture, kinds, allowedFields, requiredFields) {
  const errors = [];
  const label = `events/${fixture.name}`;
  if (fixture.parseError) return [`${label}: invalid JSON: ${fixture.parseError}`];
  const event = fixture.data;
  if (typeof event?.type !== 'string') return [`${label}: missing string field "type"`];
  if (event.type !== fixture.stem) {
    errors.push(`${label}: filename stem "${fixture.stem}" does not match type "${event.type}"`);
  }
  if (!kinds.has(event.type)) {
    errors.push(`${label}: type "${event.type}" is not a member of the EventType enum`);
  }
  for (const field of requiredFields) {
    if (!(field in event)) errors.push(`${label}: missing required field "${field}"`);
  }
  for (const field of Object.keys(event)) {
    if (!allowedFields.has(field)) errors.push(`${label}: unknown RunEvent field "${field}"`);
  }
  return errors;
}

function checkEventCoverage(files, kinds) {
  const errors = [];
  const present = new Set(files.filter(file => !file.parseError).map(file => file.stem));
  for (const kind of [...kinds].sort()) {
    if (!present.has(kind)) errors.push(`events/: EventType "${kind}" has no fixture`);
  }
  return errors;
}

function checkStep(step, index, label, frames) {
  const errors = [];
  const where = `${label} step ${index}`;
  if (!STEP_DIRECTIONS.has(step?.dir)) {
    errors.push(`${where}: "dir" must be one of c2s, s2c`);
  }
  const hasFrame = step?.frame !== undefined;
  const hasExpect = step?.expect !== undefined;
  if (hasFrame === hasExpect) {
    errors.push(`${where}: exactly one of "frame" or "expect" is required`);
    return errors;
  }
  const message = hasFrame ? step.frame : step.expect;
  if (typeof message?.type !== 'string') {
    errors.push(`${where}: message needs a string "type"`);
  } else if (!frames.has(message.type)) {
    errors.push(`${where}: unknown protocol message type "${message.type}"`);
  }
  return errors;
}

function validRequiredSetup(setup) {
  return (
    Array.isArray(setup) &&
    setup.length > 0 &&
    new Set(setup).size === setup.length &&
    setup.every(capability => typeof capability === 'string' && SETUP_CAPABILITY.test(capability))
  );
}

function scenarioShapeErrors(data, stem, label, decisions) {
  const errors = [];
  if (data?.id !== stem) {
    errors.push(`${label}: "id" must equal the filename stem "${stem}"`);
  }
  if (!SCENARIO_ROLES.has(data?.role)) {
    errors.push(`${label}: "role" must be one of control, subscribe, chat`);
  }
  if (!Array.isArray(data?.transports) || data.transports.length === 0) {
    errors.push(`${label}: "transports" must be a non-empty array`);
  }
  for (const transport of data?.transports ?? []) {
    if (!KNOWN_TRANSPORTS.has(transport)) errors.push(`${label}: unknown transport "${transport}"`);
  }
  const used = Array.isArray(data?.decisions) ? data.decisions : [];
  if (used.length === 0) errors.push(`${label}: "decisions" must tag at least one WP-* token`);
  for (const token of used) {
    if (!decisions.has(token)) {
      errors.push(`${label}: decision "${token}" is not in the wire contract`);
    }
  }
  if (data?.required_setup !== undefined && !validRequiredSetup(data.required_setup)) {
    errors.push(`${label}: "required_setup" must contain unique kebab-case capability names`);
  }
  return {errors, used};
}

function scenarioClientErrors(data, label) {
  if (data?.clients === undefined) return [];
  if (typeof data.clients !== 'object' || data.clients === null || Array.isArray(data.clients)) {
    return [`${label}: "clients" must be an object`];
  }
  const errors = [];
  const entries = Object.entries(data.clients);
  if (entries.length < 2) errors.push(`${label}: "clients" must name at least two clients`);
  for (const [name, client] of entries) {
    if (!name) errors.push(`${label}: client names must be non-empty`);
    if (!KNOWN_TRANSPORTS.has(client?.transport)) {
      errors.push(`${label}: client "${name}" has unknown transport "${client?.transport}"`);
    } else if (!Array.isArray(data.transports) || !data.transports.includes(client.transport)) {
      errors.push(`${label}: client "${name}" uses a transport not listed by the scenario`);
    }
  }
  return errors;
}

function scenarioStepErrors(data, label, frames) {
  if (!Array.isArray(data?.steps) || data.steps.length === 0) {
    return [`${label}: "steps" must be a non-empty array`];
  }
  const errors = [];
  const clients = data?.clients;
  const hasClientMap = typeof clients === 'object' && clients !== null && !Array.isArray(clients);
  for (const [index, step] of data.steps.entries()) {
    errors.push(...checkStep(step, index, label, frames));
    const where = `${label} step ${index}`;
    if (clients === undefined && step?.client !== undefined) {
      errors.push(`${where}: "client" requires a scenario "clients" map`);
    } else if (hasClientMap && !Object.hasOwn(clients, step?.client)) {
      errors.push(`${where}: "client" must name a client declared by the scenario`);
    }
  }
  return errors;
}

function checkScenario(scenario, decisions, frames) {
  const label = `scenarios/${scenario.name}`;
  if (scenario.parseError) {
    return {errors: [`${label}: invalid JSON: ${scenario.parseError}`], used: []};
  }
  const {errors, used} = scenarioShapeErrors(scenario.data, scenario.stem, label, decisions);
  errors.push(...scenarioClientErrors(scenario.data, label));
  errors.push(...scenarioStepErrors(scenario.data, label, frames));
  return {errors, used};
}

function runnerMetadataErrors(data, stem, label) {
  const errors = [];
  if (data?.id !== stem) {
    errors.push(`${label}: "id" must equal the filename stem "${stem}"`);
  }
  if (typeof data?.description !== 'string' || data.description.trim() === '') {
    errors.push(`${label}: "description" must be a non-empty string`);
  }
  for (const field of Object.keys(data ?? {})) {
    if (!RUNNER_FIELDS.has(field)) errors.push(`${label}: unknown runner field "${field}"`);
  }
  return errors;
}

function validRunnerGroups(groups) {
  return (
    typeof groups === 'object' &&
    groups !== null &&
    !Array.isArray(groups) &&
    Object.keys(groups).length > 0
  );
}

function checkRunnerGroup(group, scenarios, label, registered, scenarioIds) {
  const errors = [];
  const executed = [];
  if (!SETUP_CAPABILITY.test(group)) {
    errors.push(`${label}: group name "${group}" must be kebab-case`);
  }
  if (!Array.isArray(scenarios) || scenarios.length === 0) {
    errors.push(`${label}: group "${group}" must be a non-empty scenario array`);
    return {errors, executed};
  }
  for (const scenario of scenarios) {
    if (typeof scenario !== 'string' || scenario.length === 0) {
      errors.push(`${label}: group "${group}" must contain scenario ids`);
      continue;
    }
    if (registered.has(scenario)) {
      errors.push(`${label}: scenario "${scenario}" is registered in more than one group`);
      continue;
    }
    registered.add(scenario);
    executed.push(scenario);
    if (!scenarioIds.has(scenario)) {
      errors.push(`${label}: group "${group}" names unknown scenario "${scenario}"`);
    }
  }
  return {errors, executed};
}

function checkRunner(runner, scenarioIds) {
  const label = `runners/${runner.name}`;
  if (runner.parseError) {
    return {errors: [`${label}: invalid JSON: ${runner.parseError}`], executed: []};
  }
  const errors = runnerMetadataErrors(runner.data, runner.stem, label);
  const executed = [];
  const groups = runner.data?.groups;
  if (!validRunnerGroups(groups)) {
    errors.push(`${label}: "groups" must be a non-empty object`);
    return {errors, executed};
  }
  const registered = new Set();
  for (const [group, scenarios] of Object.entries(groups)) {
    const result = checkRunnerGroup(group, scenarios, label, registered, scenarioIds);
    errors.push(...result.errors);
    executed.push(...result.executed);
  }
  return {errors, executed};
}

function checkExecutionPartition(scenarios, executed) {
  const errors = [];
  for (const scenario of scenarios) {
    if (scenario.parseError) continue;
    const label = `scenarios/${scenario.name}`;
    const isExecuted = executed.has(scenario.stem);
    const declaresSetup = scenario.data?.required_setup !== undefined;
    if (isExecuted && declaresSetup) {
      errors.push(`${label}: runner-executed scenario must not declare "required_setup"`);
    } else if (!isExecuted && !declaresSetup) {
      errors.push(`${label}: scenario must be runner-executed or declare "required_setup"`);
    }
  }
  return errors;
}

function checkDecisionCoverage(decisions, referenced) {
  const errors = [];
  for (const token of [...decisions].sort()) {
    if (!referenced.has(token)) {
      errors.push(`wire-protocol.md: decision "${token}" is exercised by no scenario`);
    }
  }
  return errors;
}

/** Collect every corpus violation. Pure over the repo root so tests can point it at a fixture tree. */
export async function corpusErrors(root) {
  const errors = [];
  let schema;
  try {
    schema = JSON.parse(await readFile(join(root, 'clients', SCHEMA_RELATIVE), 'utf8'));
  } catch (error) {
    return [`cannot read generated schema: ${error.message}`];
  }
  const kinds = eventKinds(schema);
  const allowedFields = runEventFields(schema);
  const requiredFields = runEventRequiredFields(schema);
  const frames = frameTypes(schema);

  let decisions;
  try {
    decisions = contractDecisions(await readFile(join(root, CONTRACT_RELATIVE), 'utf8'));
  } catch (error) {
    return [`cannot read wire contract: ${error.message}`];
  }

  const corpus = join(root, CORPUS_RELATIVE);
  const events = await readJsonDir(join(corpus, 'events'));
  if (events.error) errors.push(events.error);
  for (const fixture of events.files) {
    errors.push(...checkEventFixture(fixture, kinds, allowedFields, requiredFields));
  }
  errors.push(...checkEventCoverage(events.files, kinds));

  const scenarios = await readJsonDir(join(corpus, 'scenarios'));
  if (scenarios.error) errors.push(scenarios.error);
  const scenarioIds = new Set(
    scenarios.files.filter(scenario => !scenario.parseError).map(scenario => scenario.stem),
  );
  const runners = await readJsonDir(join(corpus, RUNNERS_RELATIVE));
  if (runners.error) errors.push(runners.error);
  const executed = new Set();
  for (const runner of runners.files) {
    const result = checkRunner(runner, scenarioIds);
    errors.push(...result.errors);
    for (const scenario of result.executed) executed.add(scenario);
  }
  const referenced = new Set();
  for (const scenario of scenarios.files) {
    const result = checkScenario(scenario, decisions, frames);
    errors.push(...result.errors);
    for (const token of result.used) referenced.add(token);
  }
  errors.push(...checkExecutionPartition(scenarios.files, executed));
  errors.push(...checkDecisionCoverage(decisions, referenced));
  return errors;
}

async function main() {
  const root = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
  const errors = await corpusErrors(root);
  if (errors.length === 0) {
    console.log('Conformance corpus is complete and well formed.');
    return 0;
  }
  console.error('Conformance corpus violations:');
  for (const error of errors) console.error(`- ${error}`);
  return 1;
}

if (process.argv[1] && resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  process.exitCode = await main();
}
