import {AgentTurnAttrsSchema} from '@vibesys/proto';
import type {
  ObjectiveProjection,
  ProjectionAvailability,
  ProjectionReason,
  RunDetailFreshness,
  RunDetailProjection,
  RunDetailState,
} from './types.js';

/** The only UI-facing view: no records, protobuf Any values, or descriptor objects cross it. */
export function projectRunDetail(state: RunDetailState): RunDetailProjection {
  const freshness = freshnessOf(state);
  const scope = {kind: 'run' as const, runId: state.runId};
  const agentReasons: ProjectionReason[] = [];
  const agentIssues = state.issues.filter(
    issue => issue.attrsType === AgentTurnAttrsSchema.typeName,
  );
  if (agentIssues.some(issue => issue.reason === 'undeclared-kind')) {
    agentReasons.push('declaration-unknown');
  }
  if (agentIssues.some(issue => issue.reason === 'descriptor-unknown')) {
    agentReasons.push('descriptor-unknown');
  }
  if (
    agentIssues.some(
      issue =>
        issue.reason === 'missing-span-start' ||
        issue.reason === 'descriptor-mismatch' ||
        issue.reason === 'invalid-attributes',
    )
  ) {
    agentReasons.push('invalid-typed-fact');
  }
  if (
    state.truncation.agentEvictions > 0 ||
    state.truncation.epochEvictions > 0 ||
    state.truncation.issueEvictions > 0
  ) {
    agentReasons.push('state-truncated');
  }
  const metricReasons: ProjectionReason[] = [];
  if (
    state.truncation.metricSeriesEvictions > 0 ||
    state.truncation.epochEvictions > 0 ||
    state.metrics.some(series => series.omittedPoints > 0)
  ) {
    metricReasons.push('state-truncated');
  }
  if (state.undeclaredMetricPoints > 0) metricReasons.push('declaration-unknown');

  const currentEpoch = state.epochs.at(-1);
  const objectives: ObjectiveProjection[] =
    currentEpoch?.objectives.map(objective => ({
      ...objective,
      source: {
        kind: 'run-record',
        sequence: currentEpoch.sequence,
        epoch: currentEpoch.epoch,
      },
      scope,
    })) ?? [];

  return {
    runId: state.runId,
    freshness,
    truncation: projectionTruncation(state),
    lifecycle: state.lifecycle,
    agents: {
      source: {kind: 'run-log'},
      scope,
      freshness,
      availability: availability(agentReasons),
      values: state.agents.map(({startProof: _startProof, ...agent}) => agent),
    },
    metrics: {
      source: {kind: 'run-log'},
      scope,
      freshness,
      availability: availability(metricReasons),
      values: state.metrics.map(({epoch: _epoch, ...series}) => series),
    },
    objectives: {
      source: {kind: 'run-log'},
      scope,
      freshness,
      availability: objectiveAvailability(state, currentEpoch !== undefined),
      values: objectives,
    },
    workstreams: {
      source: {kind: 'run-log'},
      scope,
      freshness,
      availability: {kind: 'unavailable', reason: 'projection-not-installed'},
      observedSpans: undefined,
    },
    correctness: {
      source: {kind: 'run-log'},
      scope,
      freshness,
      availability: {kind: 'unavailable', reason: 'projection-not-installed'},
      observedSpans: undefined,
    },
  };
}

function projectionTruncation(state: RunDetailState): RunDetailProjection['truncation'] {
  return {
    agentEvictions: state.truncation.agentEvictions,
    epochEvictions: state.truncation.epochEvictions,
    issueEvictions: state.truncation.issueEvictions,
    metricSeriesEvictions: state.truncation.metricSeriesEvictions,
  };
}

function objectiveAvailability(
  state: RunDetailState,
  hasCurrentEpoch: boolean,
): ProjectionAvailability {
  if (hasCurrentEpoch) {
    return availability(state.truncation.epochEvictions > 0 ? ['state-truncated'] : []);
  }
  return {
    kind: 'unavailable',
    reason: state.truncation.epochEvictions > 0 ? 'state-truncated' : 'typed-fact-not-recorded',
  };
}

function freshnessOf(state: RunDetailState): RunDetailFreshness {
  switch (state.cursor.kind) {
    case 'empty':
      return {kind: 'empty'};
    case 'ready':
      return state.cursor.complete
        ? {kind: 'current', throughSequence: state.throughSequence ?? 0n}
        : {
            kind: 'catching-up',
            throughSequence: state.throughSequence,
            committedSequence: state.cursor.committedSequence,
          };
    case 'refresh-required':
      return {
        kind: 'refresh-required',
        throughSequence: state.throughSequence,
        expectedSequence: state.cursor.nextSequence,
        observedSequence: state.cursor.observedSequence,
      };
  }
}

function availability(reasons: readonly ProjectionReason[]): ProjectionAvailability {
  const unique = [...new Set(reasons)];
  return unique.length === 0 ? {kind: 'available'} : {kind: 'partial', reasons: unique};
}
