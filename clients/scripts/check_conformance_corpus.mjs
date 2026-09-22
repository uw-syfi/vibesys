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

const STEP_DIRECTIONS = new Set(['c2s', 's2c']);
const SCENARIO_ROLES = new Set(['control', 'subscribe', 'chat']);
const KNOWN_TRANSPORTS = new Set(['unix', 'websocket']);
// A control-path reply is a `Response`, which has no `type` discriminant, so scenarios name it with
// this pseudo-type. Every other frame type is derived from the schema.
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
  return {errors, used};
}

function scenarioStepErrors(data, label, frames) {
  if (!Array.isArray(data?.steps) || data.steps.length === 0) {
    return [`${label}: "steps" must be a non-empty array`];
  }
  const errors = [];
  for (const [index, step] of data.steps.entries()) {
    errors.push(...checkStep(step, index, label, frames));
  }
  return errors;
}

function checkScenario(scenario, decisions, frames) {
  const label = `scenarios/${scenario.name}`;
  if (scenario.parseError) {
    return {errors: [`${label}: invalid JSON: ${scenario.parseError}`], used: []};
  }
  const {errors, used} = scenarioShapeErrors(scenario.data, scenario.stem, label, decisions);
  errors.push(...scenarioStepErrors(scenario.data, label, frames));
  return {errors, used};
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
  const referenced = new Set();
  for (const scenario of scenarios.files) {
    const result = checkScenario(scenario, decisions, frames);
    errors.push(...result.errors);
    for (const token of result.used) referenced.add(token);
  }
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
