/**
 * Source-neutral campaign data consumed by history controllers and views.
 * Adapters validate and normalize fixture or live data into this contract.
 */

export type MetricDirection = 'maximize' | 'minimize';
export type WorkstreamOutcome = 'accepted' | 'rejected';
export type MeasurementDisposition = 'accepted' | 'rejected' | 'inconclusive';
type MeasurementKind = 'official' | 'candidate' | 'control' | 'diagnostic' | 'quick';
type GateStatus = 'passed' | 'failed' | 'skipped';

export type TurnMessage =
  | {kind: 'assistant'; content: string}
  | {kind: 'tool_call'; toolName: string; arguments: Record<string, unknown>}
  | {kind: 'tool_result'; toolName: string; content: string; isError: boolean}
  | {kind: 'result'; content: string; disposition: MeasurementDisposition};

export interface CampaignMetricDefinition {
  id: string;
  name: string;
  unit: string;
  direction: MetricDirection;
  description: string;
  benchmarkVersions: string[];
}

export interface CampaignRecord {
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
    metrics: CampaignMetricDefinition[];
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
    label: string;
    measurementKind: MeasurementKind;
    sourceOrder: number | null;
    triggeredByAgentId: string | null;
    runnerAgentId: string | null;
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

export type CampaignHeaderRecord = Pick<
  CampaignRecord,
  | 'id'
  | 'title'
  | 'summary'
  | 'provenance'
  | 'objective'
  | 'benchmarkVersions'
  | 'benchmarkVersionBoundary'
  | 'trajectories'
>;
