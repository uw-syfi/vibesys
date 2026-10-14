/**
 * Validated, frontend-local campaign metadata.
 *
 * This normalized record is independent of its source. Fixture and live
 * adapters may both produce it; presentation and history logic consume it.
 */

import {
  arrayAt,
  CampaignDecodeError,
  exactKeys,
  fail,
  objectAt,
  parseAgent,
  parseCampaignHeader,
  parseMeasurement,
  parseRecordWorkstream,
  uniqueIds,
} from './campaign-decoders.js';
import type {CampaignRecord} from './campaign-record.js';

export type {
  CampaignMetricDefinition as ReplayMetricDefinition,
  CampaignRecord,
  MeasurementDisposition,
  MetricDirection,
  TurnMessage,
  WorkstreamOutcome,
} from './campaign-record.js';

/** Compatibility name for the original fixture parser API. */
export type ReplayScenario = CampaignRecord;

interface ScenarioReferences {
  workstreams: Map<string, ReplayScenario['workstreams'][number]>;
  agents: Map<string, ReplayScenario['agents'][number]>;
  objective: ReplayScenario['objective'];
  versionIds: string[];
  boundary: ReplayScenario['benchmarkVersionBoundary'];
}

/** Parse an unknown fixture value, rejecting extra keys and invalid references. */
export function parseReplayScenario(value: unknown): ReplayScenario {
  try {
    return decodeReplayScenario(value);
  } catch (error) {
    if (error instanceof CampaignDecodeError) {
      throw new Error(`Invalid replay scenario at ${error.path}: ${error.reason}`, {cause: error});
    }
    throw error;
  }
}

function decodeReplayScenario(value: unknown): ReplayScenario {
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
  const header = parseCampaignHeader(
    {
      id: root.id,
      title: root.title,
      summary: root.summary,
      provenance: root.provenance,
      objective: root.objective,
      benchmarkVersions: root.benchmarkVersions,
      benchmarkVersionBoundary: root.benchmarkVersionBoundary,
      trajectories: root.trajectories,
    },
    '$',
  );
  const workstreams = arrayAt(root.workstreams, '$.workstreams', parseRecordWorkstream);
  const workstreamIds = uniqueIds(workstreams, '$.workstreams');
  const agents = arrayAt(root.agents, '$.agents', parseAgent);
  const agentIds = uniqueIds(agents, '$.agents');
  validateAgentWorkstreams(agents, workstreamIds);

  const measurements = arrayAt(root.measurements, '$.measurements', parseMeasurement);
  const measurementIds = uniqueIds(measurements, '$.measurements');
  void measurementIds;
  const versionIds = header.benchmarkVersions.map(version => version.id);
  validateMeasurements(
    measurements,
    workstreams,
    agents,
    header.objective,
    versionIds,
    header.benchmarkVersionBoundary,
  );
  validateTrajectories(header.trajectories, agentIds, workstreams);

  return {
    schemaVersion: 1,
    ...header,
    workstreams,
    agents,
    measurements,
  };
}

function validateAgentWorkstreams(agents: ReplayScenario['agents'], workstreamIds: string[]): void {
  const known = new Set(workstreamIds);
  for (const [index, agent] of agents.entries()) {
    const path = `$.agents[${index}].workstreamIds`;
    for (const id of agent.workstreamIds)
      if (!known.has(id)) fail(path, `unknown workstream ${id}`);
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
  validateOptionalAgentAssignment(
    references.agents,
    measurement.triggeredByAgentId,
    measurement.workstreamId,
    `${path}.triggeredByAgentId`,
  );
  validateOptionalAgentAssignment(
    references.agents,
    measurement.runnerAgentId,
    measurement.workstreamId,
    `${path}.runnerAgentId`,
  );
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

function validateOptionalAgentAssignment(
  agents: Map<string, ReplayScenario['agents'][number]>,
  agentId: string | null,
  workstreamId: string,
  path: string,
): void {
  if (agentId === null) return;
  const agent = requiredReference(agents, agentId, path, 'unknown agent');
  validateAgentAssignment(agent, workstreamId, path);
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
