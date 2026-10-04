/**
 * Validated, frontend-local campaign replay metadata.
 *
 * The event stream remains the source for live run state. This contract carries
 * the campaign facts that the current wire protocol does not represent: stable
 * workstream identity, benchmark-version boundaries, attribution, and curated
 * turn content. Parse it once at the fixture boundary before rendering.
 */

export type MetricDirection = 'maximize' | 'minimize';
export type WorkstreamOutcome = 'accepted' | 'rejected';
export type MeasurementDisposition = 'accepted' | 'rejected' | 'inconclusive';
type GateStatus = 'passed' | 'failed' | 'skipped';
export type TurnMessage =
  | {kind: 'assistant'; content: string}
  | {kind: 'tool_call'; toolName: string; arguments: Record<string, unknown>}
  | {kind: 'tool_result'; toolName: string; content: string; isError: boolean}
  | {kind: 'result'; content: string; disposition: MeasurementDisposition};

export interface ReplayMetricDefinition {
  id: string;
  name: string;
  unit: string;
  direction: MetricDirection;
  description: string;
  benchmarkVersions: string[];
}

export interface ReplayScenario {
  schemaVersion: 1;
  id: string;
  title: string;
  summary: string;
  provenance: string;
  objective: {
    title: string;
    statement: string;
    target: {metricId: string; value: number; unit: string} | null;
    constraints: string[];
    gates: {id: string; label: string; description: string}[];
    metrics: ReplayMetricDefinition[];
  };
  benchmarkVersions: {
    id: string;
    label: string;
    description: string;
    definitions: {
      metricId: string;
      unit: string;
      direction: MetricDirection;
      description: string;
    }[];
  }[];
  benchmarkVersionBoundary: {
    fromVersion: string;
    toVersion: string;
    afterSequence: number;
    reason: string;
  };
  workstreams: {
    id: string;
    title: string;
    hypothesis: string;
    startedAt: string;
    finishedAt: string;
    firstSequence: number;
    lastSequence: number;
    outcome: WorkstreamOutcome;
    outcomeSummary: string;
  }[];
  agents: {id: string; name: string; role: string; workstreamIds: string[]}[];
  measurements: {
    id: string;
    sequence: number;
    timestamp: string;
    workstreamId: string;
    triggeredByAgentId: string;
    runnerAgentId: string;
    benchmarkVersion: string;
    values: {metricId: string; value: number; unit: string}[];
    gates: {gateId: string; status: GateStatus; detail: string}[];
    disposition: MeasurementDisposition;
  }[];
  trajectories: {
    agentId: string;
    turns: {
      id: string;
      ordinal: number;
      workstreamId: string;
      startedAt: string;
      messages: TurnMessage[];
    }[];
  }[];
}

type JsonObject = Record<string, unknown> & {
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
};
type ReplayFetch = (input: string, init?: RequestInit) => Promise<Response>;

interface ScenarioReferences {
  workstreams: Map<string, ReplayScenario['workstreams'][number]>;
  agents: Map<string, ReplayScenario['agents'][number]>;
  objective: ReplayScenario['objective'];
  versionIds: string[];
  boundary: ReplayScenario['benchmarkVersionBoundary'];
}

/** Parse an unknown fixture value, rejecting extra keys and invalid references. */
export function parseReplayScenario(value: unknown): ReplayScenario {
  const root = objectAt(value, '$');
  exactKeys(root, '$', [
    'schemaVersion',
    'id',
    'title',
    'summary',
    'provenance',
    'objective',
    'benchmarkVersions',
    'benchmarkVersionBoundary',
    'workstreams',
    'agents',
    'measurements',
    'trajectories',
  ]);
  if (root.schemaVersion !== 1) fail('$.schemaVersion', 'expected 1');

  const objective = parseObjective(root.objective, '$.objective');
  const benchmarkVersions = arrayAt(
    root.benchmarkVersions,
    '$.benchmarkVersions',
    parseBenchmarkVersion,
  );
  const versionIds = uniqueIds(benchmarkVersions, '$.benchmarkVersions');
  if (versionIds.length < 2)
    fail('$.benchmarkVersions', 'expected at least two benchmark versions');
  validateMetricDefinitions(objective.metrics, benchmarkVersions, '$.objective.metrics');

  const boundary = parseBoundary(root.benchmarkVersionBoundary, '$.benchmarkVersionBoundary');
  if (!versionIds.includes(boundary.fromVersion))
    fail('$.benchmarkVersionBoundary.fromVersion', 'unknown benchmark version');
  if (!versionIds.includes(boundary.toVersion))
    fail('$.benchmarkVersionBoundary.toVersion', 'unknown benchmark version');
  if (boundary.fromVersion === boundary.toVersion)
    fail('$.benchmarkVersionBoundary', 'versions must differ');
  validateVersionDefinitions(objective.metrics, benchmarkVersions, '$.benchmarkVersions');

  const workstreams = arrayAt(root.workstreams, '$.workstreams', parseWorkstream);
  const workstreamIds = uniqueIds(workstreams, '$.workstreams');
  const agents = arrayAt(root.agents, '$.agents', parseAgent);
  const agentIds = uniqueIds(agents, '$.agents');
  validateAgentWorkstreams(agents, workstreamIds);

  const measurements = arrayAt(root.measurements, '$.measurements', parseMeasurement);
  const measurementIds = uniqueIds(measurements, '$.measurements');
  void measurementIds;
  validateMeasurements(measurements, workstreams, agents, objective, versionIds, boundary);
  const trajectories = arrayAt(root.trajectories, '$.trajectories', parseTrajectory);
  uniqueBy(trajectories, '$.trajectories', trajectory => trajectory.agentId);
  validateTrajectories(trajectories, agentIds, workstreams);

  return {
    schemaVersion: 1,
    id: stringAt(root.id, '$.id'),
    title: stringAt(root.title, '$.title'),
    summary: stringAt(root.summary, '$.summary'),
    provenance: stringAt(root.provenance, '$.provenance'),
    objective,
    benchmarkVersions,
    benchmarkVersionBoundary: boundary,
    workstreams,
    agents,
    measurements,
    trajectories,
  };
}

/** Fetch and validate a scenario document. The fetch implementation is injectable. */
export async function loadReplayScenario(
  url: string,
  fetchScenario: ReplayFetch = globalThis.fetch,
): Promise<ReplayScenario> {
  const response = await fetchScenario(url);
  if (!response.ok) throw new Error(`Replay scenario request failed with ${response.status}`);
  let value: unknown;
  try {
    value = await response.json();
  } catch (reason) {
    throw new Error('Replay scenario contains invalid JSON', {cause: reason});
  }
  return parseReplayScenario(value);
}

function parseObjective(value: unknown, path: string): ReplayScenario['objective'] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['title', 'statement', 'target', 'constraints', 'gates', 'metrics']);
  const targetPath = `${path}.target`;
  let target: ReplayScenario['objective']['target'] = null;
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
    if (!result.metrics.some(metric => metric.id === result.target?.metricId)) {
      fail(`${targetPath}.metricId`, 'unknown objective metric');
    }
    const targetMetric = result.metrics.find(metric => metric.id === result.target?.metricId);
    if (targetMetric?.unit !== result.target.unit)
      fail(`${targetPath}.unit`, 'does not match metric definition');
  }
  return result;
}

function parseGateDefinition(
  value: unknown,
  path: string,
): ReplayScenario['objective']['gates'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['id', 'label', 'description']);
  return {
    id: stringAt(object.id, `${path}.id`),
    label: stringAt(object.label, `${path}.label`),
    description: stringAt(object.description, `${path}.description`),
  };
}

function parseMetricDefinition(value: unknown, path: string): ReplayMetricDefinition {
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
): ReplayScenario['benchmarkVersions'][number] {
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

function parseBoundary(value: unknown, path: string): ReplayScenario['benchmarkVersionBoundary'] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['fromVersion', 'toVersion', 'afterSequence', 'reason']);
  return {
    fromVersion: stringAt(object.fromVersion, `${path}.fromVersion`),
    toVersion: stringAt(object.toVersion, `${path}.toVersion`),
    afterSequence: positiveIntegerAt(object.afterSequence, `${path}.afterSequence`, true),
    reason: stringAt(object.reason, `${path}.reason`),
  };
}

function parseWorkstream(value: unknown, path: string): ReplayScenario['workstreams'][number] {
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

function parseAgent(value: unknown, path: string): ReplayScenario['agents'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, ['id', 'name', 'role', 'workstreamIds']);
  return {
    id: stringAt(object.id, `${path}.id`),
    name: stringAt(object.name, `${path}.name`),
    role: stringAt(object.role, `${path}.role`),
    workstreamIds: arrayAt(object.workstreamIds, `${path}.workstreamIds`, stringAt),
  };
}

function parseMeasurement(value: unknown, path: string): ReplayScenario['measurements'][number] {
  const object = objectAt(value, path);
  exactKeys(object, path, [
    'id',
    'sequence',
    'timestamp',
    'workstreamId',
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
    triggeredByAgentId: stringAt(object.triggeredByAgentId, `${path}.triggeredByAgentId`),
    runnerAgentId: stringAt(object.runnerAgentId, `${path}.runnerAgentId`),
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

function parseTrajectory(value: unknown, path: string): ReplayScenario['trajectories'][number] {
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
    if (typeof object.isError !== 'boolean') fail(`${path}.isError`, 'expected boolean');
    return {
      kind,
      toolName: stringAt(object.toolName, `${path}.toolName`),
      content: stringAt(object.content, `${path}.content`),
      isError: object.isError,
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
  metrics: ReplayMetricDefinition[],
  versions: ReplayScenario['benchmarkVersions'],
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
  metrics: ReplayMetricDefinition[],
  versions: ReplayScenario['benchmarkVersions'],
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

function validateAgentWorkstreams(agents: ReplayScenario['agents'], workstreamIds: string[]): void {
  const known = new Set(workstreamIds);
  for (const [index, agent] of agents.entries()) {
    const path = `$.agents[${index}].workstreamIds`;
    const refs = uniqueStrings(agent.workstreamIds, path);
    if (refs.length === 0) fail(path, 'must link the agent to at least one workstream');
    for (const id of refs) if (!known.has(id)) fail(path, `unknown workstream ${id}`);
  }
}

function validateMeasurements(
  measurements: ReplayScenario['measurements'],
  workstreams: ReplayScenario['workstreams'],
  agents: ReplayScenario['agents'],
  objective: ReplayScenario['objective'],
  versionIds: string[],
  boundary: ReplayScenario['benchmarkVersionBoundary'],
): void {
  const workstreamMap = new Map(workstreams.map(workstream => [workstream.id, workstream]));
  const agentMap = new Map(agents.map(agent => [agent.id, agent]));
  const references: ScenarioReferences = {
    workstreams: workstreamMap,
    agents: agentMap,
    objective,
    versionIds,
    boundary,
  };
  for (const [index, measurement] of measurements.entries()) {
    validateMeasurement(measurement, index, references);
  }
}

function validateMeasurement(
  measurement: ReplayScenario['measurements'][number],
  index: number,
  references: ScenarioReferences,
): void {
  const path = `$.measurements[${index}]`;
  const workstream = requiredReference(
    references.workstreams,
    measurement.workstreamId,
    `${path}.workstreamId`,
    'unknown workstream',
  );
  const triggerAgent = requiredReference(
    references.agents,
    measurement.triggeredByAgentId,
    `${path}.triggeredByAgentId`,
    'unknown agent',
  );
  const runnerAgent = requiredReference(
    references.agents,
    measurement.runnerAgentId,
    `${path}.runnerAgentId`,
    'unknown agent',
  );
  validateAgentAssignment(triggerAgent, measurement.workstreamId, `${path}.triggeredByAgentId`);
  validateAgentAssignment(runnerAgent, measurement.workstreamId, `${path}.runnerAgentId`);
  validateMeasurementInterval(measurement, workstream, path);
  validateMeasurementVersion(measurement, references.versionIds, references.boundary, path);
  validateMeasurementValues(measurement, references.objective, path);
  validateMeasurementGates(measurement, references.objective, path);
}

function requiredReference<T>(
  values: Map<string, T>,
  id: string,
  path: string,
  message: string,
): T {
  const value = values.get(id);
  if (value === undefined) fail(path, message);
  return value;
}

function validateAgentAssignment(
  agent: ReplayScenario['agents'][number],
  workstreamId: string,
  path: string,
): void {
  if (!agent.workstreamIds.includes(workstreamId))
    fail(path, 'agent is not linked to this workstream');
}

function validateMeasurementInterval(
  measurement: ReplayScenario['measurements'][number],
  workstream: ReplayScenario['workstreams'][number],
  path: string,
): void {
  if (
    measurement.sequence < workstream.firstSequence ||
    measurement.sequence > workstream.lastSequence
  ) {
    fail(`${path}.sequence`, 'outside workstream lifecycle interval');
  }
  const measuredAt = Date.parse(measurement.timestamp);
  if (
    measuredAt < Date.parse(workstream.startedAt) ||
    measuredAt > Date.parse(workstream.finishedAt)
  ) {
    fail(`${path}.timestamp`, 'outside workstream lifecycle interval');
  }
}

function validateMeasurementVersion(
  measurement: ReplayScenario['measurements'][number],
  versionIds: string[],
  boundary: ReplayScenario['benchmarkVersionBoundary'],
  path: string,
): void {
  if (!versionIds.includes(measurement.benchmarkVersion))
    fail(`${path}.benchmarkVersion`, 'unknown benchmark version');
  const expected =
    measurement.sequence <= boundary.afterSequence ? boundary.fromVersion : boundary.toVersion;
  if (measurement.benchmarkVersion !== expected) {
    fail(`${path}.benchmarkVersion`, `expected ${expected} at sequence ${measurement.sequence}`);
  }
}

function validateMeasurementValues(
  measurement: ReplayScenario['measurements'][number],
  objective: ReplayScenario['objective'],
  path: string,
): void {
  const metrics = new Map(objective.metrics.map(metric => [metric.id, metric]));
  for (const [index, value] of measurement.values.entries()) {
    const valuePath = `${path}.values[${index}]`;
    const metric = requiredReference(
      metrics,
      value.metricId,
      `${valuePath}.metricId`,
      'unknown metric',
    );
    if (value.unit !== metric.unit) fail(`${valuePath}.unit`, 'does not match metric definition');
    if (!metric.benchmarkVersions.includes(measurement.benchmarkVersion)) {
      fail(`${valuePath}.metricId`, 'not defined for this benchmark version');
    }
  }
}

function validateMeasurementGates(
  measurement: ReplayScenario['measurements'][number],
  objective: ReplayScenario['objective'],
  path: string,
): void {
  const gateIds = new Set(objective.gates.map(gate => gate.id));
  for (const [index, gate] of measurement.gates.entries()) {
    if (!gateIds.has(gate.gateId)) fail(`${path}.gates[${index}].gateId`, 'unknown objective gate');
  }
}

function validateTrajectories(
  trajectories: ReplayScenario['trajectories'],
  agentIds: string[],
  workstreams: ReplayScenario['workstreams'],
): void {
  const knownAgents = new Set(agentIds);
  const workstreamMap = new Map(workstreams.map(workstream => [workstream.id, workstream]));
  for (const [index, trajectory] of trajectories.entries()) {
    const path = `$.trajectories[${index}]`;
    if (!knownAgents.has(trajectory.agentId)) fail(`${path}.agentId`, 'unknown agent');
    for (const [turnIndex, turn] of trajectory.turns.entries()) {
      const turnPath = `${path}.turns[${turnIndex}]`;
      const workstream = workstreamMap.get(turn.workstreamId);
      if (workstream === undefined) fail(`${turnPath}.workstreamId`, 'unknown workstream');
      const turnTime = Date.parse(turn.startedAt);
      if (
        turnTime < Date.parse(workstream.startedAt) ||
        turnTime > Date.parse(workstream.finishedAt)
      )
        fail(`${turnPath}.startedAt`, 'outside workstream lifecycle interval');
    }
  }
}

function objectAt(value: unknown, path: string): JsonObject {
  if (typeof value !== 'object' || value === null || Array.isArray(value))
    return fail(path, 'expected object');
  return value as JsonObject;
}

function exactKeys(value: JsonObject, path: string, keys: readonly string[]): void {
  const expected = new Set(keys);
  for (const key of Object.keys(value))
    if (!expected.has(key)) fail(`${path}.${key}`, 'unknown key');
  for (const key of keys) if (!(key in value)) fail(`${path}.${key}`, 'missing required key');
}

function stringAt(value: unknown, path: string): string {
  if (typeof value !== 'string' || value.trim().length === 0)
    return fail(path, 'expected non-empty string');
  return value;
}

function numberAt(value: unknown, path: string): number {
  if (typeof value !== 'number' || !Number.isFinite(value))
    return fail(path, 'expected finite number');
  return value;
}

function positiveIntegerAt(value: unknown, path: string, allowZero = false): number {
  const minimum = allowZero ? 0 : 1;
  if (typeof value !== 'number' || !Number.isInteger(value) || value < minimum)
    return fail(path, `expected integer >= ${minimum}`);
  return value;
}

function timestampAt(value: unknown, path: string): string {
  const timestamp = stringAt(value, path);
  if (
    !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(timestamp) ||
    Number.isNaN(Date.parse(timestamp))
  )
    fail(path, 'expected UTC ISO timestamp');
  return timestamp;
}

function enumAt<const T extends readonly string[]>(
  value: unknown,
  path: string,
  allowed: T,
): T[number] {
  if (typeof value !== 'string' || !allowed.includes(value))
    return fail(path, `expected one of ${allowed.join(', ')}`);
  return value as T[number];
}

function arrayAt<T>(
  value: unknown,
  path: string,
  parse: (item: unknown, itemPath: string) => T,
): T[] {
  if (!Array.isArray(value)) return fail(path, 'expected array');
  return value.map((item, index) => parse(item, `${path}[${index}]`));
}

function uniqueIds<T extends {id: string}>(values: T[], path: string): string[] {
  return uniqueBy(values, path, item => item.id);
}

function uniqueBy<T>(values: T[], path: string, identify: (item: T) => string): string[] {
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

function fail(path: string, reason: string): never {
  throw new Error(`Invalid replay scenario at ${path}: ${reason}`);
}
