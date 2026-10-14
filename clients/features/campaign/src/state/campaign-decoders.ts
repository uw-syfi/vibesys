/**
 * Structural campaign-data decoders shared by the finished-record and delta-frame boundaries.
 *
 * These functions validate one value at a time. Relationships that require a complete record or
 * an ordered stream remain with the parser or fold that owns that context.
 */
import type {
  CampaignHeaderRecord,
  CampaignMetricDefinition,
  CampaignRecord,
  TurnMessage,
} from './campaign-record.js';

export class CampaignDecodeError extends Error {
  constructor(
    readonly path: string,
    readonly reason: string,
  ) {
    super(`${path}: ${reason}`);
  }
}

export function fail(path: string, reason: string): never {
  throw new CampaignDecodeError(path, reason);
}

export type CampaignJsonObject = Record<string, unknown> & {
  schemaVersion?: unknown;
  id?: unknown;
  title?: unknown;
  summary?: unknown;
  provenance?: unknown;
  objective?: unknown;
  benchmarkVersions?: unknown;
  benchmarkVersionBoundary?: unknown;
  workstreams?: unknown;
  agents?: unknown;
  measurements?: unknown;
  trajectories?: unknown;
  target?: unknown;
  metricId?: unknown;
  value?: unknown;
  unit?: unknown;
  constraints?: unknown;
  gates?: unknown;
  metrics?: unknown;
  label?: unknown;
  description?: unknown;
  name?: unknown;
  statement?: unknown;
  direction?: unknown;
  definitions?: unknown;
  fromVersion?: unknown;
  toVersion?: unknown;
  afterSequence?: unknown;
  reason?: unknown;
  hypothesis?: unknown;
  startedAt?: unknown;
  finishedAt?: unknown;
  firstSequence?: unknown;
  lastSequence?: unknown;
  outcome?: unknown;
  outcomeSummary?: unknown;
  role?: unknown;
  workstreamIds?: unknown;
  sequence?: unknown;
  timestamp?: unknown;
  workstreamId?: unknown;
  measurementKind?: unknown;
  sourceOrder?: unknown;
  triggeredByAgentId?: unknown;
  runnerAgentId?: unknown;
  benchmarkVersion?: unknown;
  values?: unknown;
  disposition?: unknown;
  gateId?: unknown;
  status?: unknown;
  detail?: unknown;
  agentId?: unknown;
  turns?: unknown;
  ordinal?: unknown;
  messages?: unknown;
  kind?: unknown;
  content?: unknown;
  toolName?: unknown;
  arguments?: unknown;
  isError?: unknown;
  seq?: unknown;
  header?: unknown;
  agent?: unknown;
  workstream?: unknown;
  measurement?: unknown;
  tokens?: unknown;
  phase?: unknown;
  active?: unknown;
};

export function objectAt(value: unknown, path: string): CampaignJsonObject {
  if (typeof value !== 'object' || value === null || Array.isArray(value))
    return fail(path, 'expected object');
  return value as CampaignJsonObject;
}

export function exactKeys(value: CampaignJsonObject, path: string, keys: readonly string[]): void {
  const expected = new Set(keys);
  for (const key of Object.keys(value))
    if (!expected.has(key)) fail(`${path}.${key}`, 'unknown key');
  for (const key of keys) if (!(key in value)) fail(`${path}.${key}`, 'missing required key');
}

export function stringAt(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.trim().length === 0)
    return fail(path, 'expected non-empty string');
  return value;
}

/** A string whose empty value has domain meaning, such as an unfinished outcome summary. */
export function textAt(value: unknown, path: string): string {
  if (typeof value !== 'string') return fail(path, 'expected string');
  return value;
}

export function nullableStringAt(value: unknown, path: string): string | null {
  return value === null ? null : stringAt(value, path);
}

export function numberAt(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value))
    return fail(path, 'expected finite number');
  return value;
}

export function positiveIntegerAt(value: unknown, path: string, allowZero = false): number {
  const minimum = allowZero ? 0 : 1;
  if (typeof value !== 'number' || !Number.isSafeInteger(value) || value < minimum)
    return fail(path, `expected integer >= ${minimum} within safe range`);
  return value;
}

export function nullablePositiveIntegerAt(value: unknown, path: string): number | null {
  return value === null ? null : positiveIntegerAt(value, path);
}

export function timestampAt(value: unknown, path: string): string {
  const timestamp = stringAt(value, path);
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d+)?Z$/.exec(timestamp);
  if (match === null) fail(path, 'expected UTC ISO timestamp');
  const instant = new Date(timestamp);
  if (
    Number.isNaN(instant.getTime()) ||
    instant.getUTCFullYear() !== Number(match[1]) ||
    instant.getUTCMonth() + 1 !== Number(match[2]) ||
    instant.getUTCDate() !== Number(match[3]) ||
    instant.getUTCHours() !== Number(match[4]) ||
    instant.getUTCMinutes() !== Number(match[5]) ||
    instant.getUTCSeconds() !== Number(match[6])
  ) {
    fail(path, 'expected UTC ISO timestamp');
  }
  return timestamp;
}

export function nullableTimestampAt(value: unknown, path: string): string | null {
  return value === null ? null : timestampAt(value, path);
}

export function booleanAt(value: unknown, path: string): boolean {
  if (typeof value !== 'boolean') return fail(path, 'expected boolean');
  return value;
}

export function enumAt<const T extends readonly string[]>(
  value: unknown,
  path: string,
  allowed: T,
): T[number] {
  if (typeof value !== 'string' || !allowed.includes(value))
    return fail(path, `expected one of ${allowed.join(', ')}`);
  return value as T[number];
}

export function arrayAt<T>(
  value: unknown,
  path: string,
  parse: (item: unknown, itemPath: string) => T,
): T[] {
  if (!Array.isArray(value)) return fail(path, 'expected array');
  return value.map((item, index) => parse(item, `${path}[${index}]`));
}

export function uniqueIds<T extends {id: string}>(values: T[], path: string): string[] {
  return uniqueBy(values, path, item => item.id);
}

export function uniqueBy<T>(values: T[], path: string, identify: (item: T) => string): string[] {
  const seen = new Set<string>();
  for (const [index, value] of values.entries()) {
    const id = identify(value);
    if (seen.has(id)) fail(`${path}[${index}]`, `duplicate id ${id}`);
    seen.add(id);
  }
  return [...seen];
}

function uniqueStrings(values: string[], path: string): string[] {
  const seen = new Set<string>();
  for (const [index, value] of values.entries()) {
    if (seen.has(value)) fail(`${path}[${index}]`, `duplicate value ${value}`);
    seen.add(value);
  }
  return [...seen];
}

export function parseCampaignHeader(value: unknown, path: string): CampaignHeaderRecord {
  const object = objectAt(value, path);
  exactKeys(object, path, [
    'id',
    'title',
    'summary',
    'provenance',
    'objective',
    'benchmarkVersions',
    'benchmarkVersionBoundary',
    'trajectories',
  ]);
  const objective = parseObjective(object.objective, `${path}.objective`);
  const benchmarkVersions = arrayAt(
    object.benchmarkVersions,
    `${path}.benchmarkVersions`,
    parseBenchmarkVersion,
  );
  const versionIds = uniqueIds(benchmarkVersions, `${path}.benchmarkVersions`);
  if (versionIds.length < 2)
    fail(`${path}.benchmarkVersions`, 'expected at least two benchmark versions');
  validateMetricDefinitions(objective.metrics, benchmarkVersions, `${path}.objective.metrics`);

  const boundary = parseBoundary(
    object.benchmarkVersionBoundary,
    `${path}.benchmarkVersionBoundary`,
  );
  if (!versionIds.includes(boundary.fromVersion))
    fail(`${path}.benchmarkVersionBoundary.fromVersion`, 'unknown benchmark version');
  if (!versionIds.includes(boundary.toVersion))
    fail(`${path}.benchmarkVersionBoundary.toVersion`, 'unknown benchmark version');
  if (boundary.fromVersion === boundary.toVersion)
    fail(`${path}.benchmarkVersionBoundary`, 'versions must differ');
  validateVersionDefinitions(objective.metrics, benchmarkVersions, `${path}.benchmarkVersions`);

  const trajectories = arrayAt(object.trajectories, `${path}.trajectories`, parseTrajectory);
  uniqueBy(trajectories, `${path}.trajectories`, trajectory => trajectory.agentId);
  return {
    id: stringAt(object.id, `${path}.id`),
    title: stringAt(object.title, `${path}.title`),
    summary: stringAt(object.summary, `${path}.summary`),
    provenance: stringAt(object.provenance, `${path}.provenance`),
    objective,
    benchmarkVersions,
    benchmarkVersionBoundary: boundary,
    trajectories,
  };
}

function parseObjective(value: unknown, path: string): CampaignRecord['objective'] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['title', 'statement', 'target', 'constraints', 'gates', 'metrics']);
  const targetPath = `${path}.target`;
  let target: CampaignRecord['objective']['target'] = null;
  if (object.target !== null) {
    const targetObject = objectAt(object.target, targetPath);
    exactKeys(targetObject, targetPath, ['metricId', 'value', 'unit']);
    target = {
      metricId: stringAt(targetObject.metricId, `${targetPath}.metricId`),
      value: numberAt(targetObject.value, `${targetPath}.value`),
      unit: stringAt(targetObject.unit, `${targetPath}.unit`),
    };
  }
  const result = {
    title: stringAt(object.title, `${path}.title`),
    statement: stringAt(object.statement, `${path}.statement`),
    target,
    constraints: arrayAt(object.constraints, `${path}.constraints`, stringAt),
    gates: arrayAt(object.gates, `${path}.gates`, parseGateDefinition),
    metrics: arrayAt(object.metrics, `${path}.metrics`, parseMetricDefinition),
  };
  uniqueBy(result.gates, `${path}.gates`, gate => gate.id);
  uniqueBy(result.metrics, `${path}.metrics`, metric => metric.id);
  if (result.target !== null) {
    const targetMetric = result.metrics.find(metric => metric.id === result.target?.metricId);
    if (targetMetric === undefined) fail(`${targetPath}.metricId`, 'unknown objective metric');
    if (targetMetric.unit !== result.target.unit)
      fail(`${targetPath}.unit`, 'does not match metric definition');
  }
  return result;
}

function parseGateDefinition(
  value: unknown,
  path: string,
): CampaignRecord['objective']['gates'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['id', 'label', 'description']);
  return {
    id: stringAt(object.id, `${path}.id`),
    label: stringAt(object.label, `${path}.label`),
    description: stringAt(object.description, `${path}.description`),
  };
}

function parseMetricDefinition(value: unknown, path: string): CampaignMetricDefinition {
  const object = objectAt(value, path);
  exactKeys(object, path, ['id', 'name', 'unit', 'direction', 'description', 'benchmarkVersions']);
  return {
    id: stringAt(object.id, `${path}.id`),
    name: stringAt(object.name, `${path}.name`),
    unit: stringAt(object.unit, `${path}.unit`),
    direction: enumAt(object.direction, `${path}.direction`, ['maximize', 'minimize']),
    description: stringAt(object.description, `${path}.description`),
    benchmarkVersions: arrayAt(object.benchmarkVersions, `${path}.benchmarkVersions`, stringAt),
  };
}

function parseBenchmarkVersion(
  value: unknown,
  path: string,
): CampaignRecord['benchmarkVersions'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['id', 'label', 'description', 'definitions']);
  const definitions = arrayAt(object.definitions, `${path}.definitions`, (item, itemPath) => {
    const definition = objectAt(item, itemPath);
    exactKeys(definition, itemPath, ['metricId', 'unit', 'direction', 'description']);
    return {
      metricId: stringAt(definition.metricId, `${itemPath}.metricId`),
      unit: stringAt(definition.unit, `${itemPath}.unit`),
      direction: enumAt(definition.direction, `${itemPath}.direction`, ['maximize', 'minimize']),
      description: stringAt(definition.description, `${itemPath}.description`),
    };
  });
  uniqueBy(definitions, `${path}.definitions`, definition => definition.metricId);
  return {
    id: stringAt(object.id, `${path}.id`),
    label: stringAt(object.label, `${path}.label`),
    description: stringAt(object.description, `${path}.description`),
    definitions,
  };
}

function parseBoundary(value: unknown, path: string): CampaignRecord['benchmarkVersionBoundary'] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['fromVersion', 'toVersion', 'afterSequence', 'reason']);
  return {
    fromVersion: stringAt(object.fromVersion, `${path}.fromVersion`),
    toVersion: stringAt(object.toVersion, `${path}.toVersion`),
    afterSequence: positiveIntegerAt(object.afterSequence, `${path}.afterSequence`, true),
    reason: stringAt(object.reason, `${path}.reason`),
  };
}

export function parseRecordWorkstream(
  value: unknown,
  path: string,
): CampaignRecord['workstreams'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, [
    'id',
    'title',
    'hypothesis',
    'startedAt',
    'finishedAt',
    'firstSequence',
    'lastSequence',
    'outcome',
    'outcomeSummary',
  ]);
  const startedAt = timestampAt(object.startedAt, `${path}.startedAt`);
  const finishedAt = timestampAt(object.finishedAt, `${path}.finishedAt`);
  if (Date.parse(finishedAt) < Date.parse(startedAt))
    fail(`${path}.finishedAt`, 'must not precede startedAt');
  const firstSequence = positiveIntegerAt(object.firstSequence, `${path}.firstSequence`);
  const lastSequence = positiveIntegerAt(object.lastSequence, `${path}.lastSequence`);
  if (lastSequence < firstSequence) fail(`${path}.lastSequence`, 'must not precede firstSequence');
  return {
    id: stringAt(object.id, `${path}.id`),
    title: stringAt(object.title, `${path}.title`),
    hypothesis: stringAt(object.hypothesis, `${path}.hypothesis`),
    startedAt,
    finishedAt,
    firstSequence,
    lastSequence,
    outcome: enumAt(object.outcome, `${path}.outcome`, ['accepted', 'rejected']),
    outcomeSummary: stringAt(object.outcomeSummary, `${path}.outcomeSummary`),
  };
}

export function parseAgent(value: unknown, path: string): CampaignRecord['agents'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['id', 'name', 'role', 'workstreamIds']);
  const workstreamIds = arrayAt(object.workstreamIds, `${path}.workstreamIds`, stringAt);
  const uniqueWorkstreamIds = uniqueStrings(workstreamIds, `${path}.workstreamIds`);
  if (uniqueWorkstreamIds.length === 0)
    fail(`${path}.workstreamIds`, 'must link the agent to at least one workstream');
  return {
    id: stringAt(object.id, `${path}.id`),
    name: stringAt(object.name, `${path}.name`),
    role: stringAt(object.role, `${path}.role`),
    workstreamIds,
  };
}

export function parseMeasurement(
  value: unknown,
  path: string,
): CampaignRecord['measurements'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, [
    'id',
    'sequence',
    'timestamp',
    'workstreamId',
    'label',
    'measurementKind',
    'sourceOrder',
    'triggeredByAgentId',
    'runnerAgentId',
    'benchmarkVersion',
    'values',
    'gates',
    'disposition',
  ]);
  const values = arrayAt(object.values, `${path}.values`, (item, itemPath) => {
    const entry = objectAt(item, itemPath);
    exactKeys(entry, itemPath, ['metricId', 'value', 'unit']);
    return {
      metricId: stringAt(entry.metricId, `${itemPath}.metricId`),
      value: numberAt(entry.value, `${itemPath}.value`),
      unit: stringAt(entry.unit, `${itemPath}.unit`),
    };
  });
  uniqueBy(values, `${path}.values`, entry => entry.metricId);
  const gates = arrayAt(object.gates, `${path}.gates`, (item, itemPath) => {
    const entry = objectAt(item, itemPath);
    exactKeys(entry, itemPath, ['gateId', 'status', 'detail']);
    return {
      gateId: stringAt(entry.gateId, `${itemPath}.gateId`),
      status: enumAt(entry.status, `${itemPath}.status`, ['passed', 'failed', 'skipped']),
      detail: stringAt(entry.detail, `${itemPath}.detail`),
    };
  });
  uniqueBy(gates, `${path}.gates`, entry => entry.gateId);
  return {
    id: stringAt(object.id, `${path}.id`),
    sequence: positiveIntegerAt(object.sequence, `${path}.sequence`),
    timestamp: timestampAt(object.timestamp, `${path}.timestamp`),
    workstreamId: stringAt(object.workstreamId, `${path}.workstreamId`),
    label: stringAt(object.label, `${path}.label`),
    measurementKind: enumAt(object.measurementKind, `${path}.measurementKind`, [
      'official',
      'candidate',
      'control',
      'diagnostic',
      'quick',
    ]),
    sourceOrder: nullablePositiveIntegerAt(object.sourceOrder, `${path}.sourceOrder`),
    triggeredByAgentId: nullableStringAt(object.triggeredByAgentId, `${path}.triggeredByAgentId`),
    runnerAgentId: nullableStringAt(object.runnerAgentId, `${path}.runnerAgentId`),
    benchmarkVersion: stringAt(object.benchmarkVersion, `${path}.benchmarkVersion`),
    values,
    gates,
    disposition: enumAt(object.disposition, `${path}.disposition`, [
      'accepted',
      'rejected',
      'inconclusive',
    ]),
  };
}

function parseTrajectory(value: unknown, path: string): CampaignRecord['trajectories'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['agentId', 'turns']);
  const turns = arrayAt(object.turns, `${path}.turns`, (item, itemPath) => {
    const turn = objectAt(item, itemPath);
    exactKeys(turn, itemPath, ['id', 'ordinal', 'workstreamId', 'startedAt', 'messages']);
    const messages = arrayAt(turn.messages, `${itemPath}.messages`, parseTurnMessage);
    if (messages.length === 0) fail(`${itemPath}.messages`, 'must contain at least one message');
    return {
      id: stringAt(turn.id, `${itemPath}.id`),
      ordinal: positiveIntegerAt(turn.ordinal, `${itemPath}.ordinal`),
      workstreamId: stringAt(turn.workstreamId, `${itemPath}.workstreamId`),
      startedAt: timestampAt(turn.startedAt, `${itemPath}.startedAt`),
      messages,
    };
  });
  uniqueBy(turns, `${path}.turns`, turn => turn.id);
  if (turns.some((turn, index) => turn.ordinal !== index + 1))
    fail(`${path}.turns`, 'ordinals must be contiguous and ordered from 1');
  return {agentId: stringAt(object.agentId, `${path}.agentId`), turns};
}

function parseTurnMessage(value: unknown, path: string): TurnMessage {
  const object = objectAt(value, path);
  const kind = stringAt(object.kind, `${path}.kind`);
  if (kind === 'assistant') {
    exactKeys(object, path, ['kind', 'content']);
    return {kind, content: stringAt(object.content, `${path}.content`)};
  }
  if (kind === 'tool_call') {
    exactKeys(object, path, ['kind', 'toolName', 'arguments']);
    const args = objectAt(object.arguments, `${path}.arguments`);
    return {kind, toolName: stringAt(object.toolName, `${path}.toolName`), arguments: {...args}};
  }
  if (kind === 'tool_result') {
    exactKeys(object, path, ['kind', 'toolName', 'content', 'isError']);
    return {
      kind,
      toolName: stringAt(object.toolName, `${path}.toolName`),
      content: stringAt(object.content, `${path}.content`),
      isError: booleanAt(object.isError, `${path}.isError`),
    };
  }
  if (kind === 'result') {
    exactKeys(object, path, ['kind', 'content', 'disposition']);
    return {
      kind,
      content: stringAt(object.content, `${path}.content`),
      disposition: enumAt(object.disposition, `${path}.disposition`, [
        'accepted',
        'rejected',
        'inconclusive',
      ]),
    };
  }
  return fail(`${path}.kind`, 'expected assistant, tool_call, tool_result, or result');
}

function validateMetricDefinitions(
  metrics: CampaignMetricDefinition[],
  versions: CampaignRecord['benchmarkVersions'],
  path: string,
): void {
  const ids = new Set(versions.map(version => version.id));
  for (const [index, metric] of metrics.entries()) {
    const versionsPath = `${path}[${index}].benchmarkVersions`;
    const refs = uniqueStrings(metric.benchmarkVersions, versionsPath);
    if (refs.length === 0) fail(versionsPath, 'must name at least one benchmark version');
    for (const version of refs)
      if (!ids.has(version)) fail(versionsPath, `unknown benchmark version ${version}`);
  }
}

function validateVersionDefinitions(
  metrics: CampaignMetricDefinition[],
  versions: CampaignRecord['benchmarkVersions'],
  path: string,
): void {
  const metricMap = new Map(metrics.map(metric => [metric.id, metric]));
  for (const [index, version] of versions.entries()) {
    for (const [definitionIndex, definition] of version.definitions.entries()) {
      const definitionPath = `${path}[${index}].definitions[${definitionIndex}]`;
      const metric = metricMap.get(definition.metricId);
      if (metric === undefined) fail(`${definitionPath}.metricId`, 'unknown metric');
      if (!metric.benchmarkVersions.includes(version.id))
        fail(`${definitionPath}.metricId`, 'metric does not include this benchmark version');
      if (metric.unit !== definition.unit || metric.direction !== definition.direction)
        fail(definitionPath, 'does not match objective metric definition');
    }
  }
}
