import {AgentTurnAttrsSchema, GateAttrsSchema, WorkstreamAttrsSchema} from '@vibesys/proto';
import type {
  ObjectiveProjection,
  ProjectionAvailability,
  ProjectionReason,
  ProjectionSet,
  RunDetailFreshness,
  RunDetailProjection,
  RunDetailState,
  UnprojectedSpanProjection,
} from './types.js';

/** The only UI-facing view: no records, protobuf Any values, or descriptor objects cross it. */
export function projectRunDetail(state: RunDetailState): RunDetailProjection {
  const freshness = freshnessOf(state);
  const scope = {kind: 'run' as const, runId: state.runId};
  const agentReasons = spanReasons(
    state,
    AgentTurnAttrsSchema.typeName,
    state.truncation.agentEvictions,
  );
  const workstreamReasons = spanReasons(
    state,
    WorkstreamAttrsSchema.typeName,
    state.truncation.workstreamEvictions,
  );
  const correctnessReasons = spanReasons(
    state,
    GateAttrsSchema.typeName,
    state.truncation.correctnessGateEvictions,
  );
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
      availability: availability(workstreamReasons),
      values: state.workstreams.map(({startProof: _startProof, ...workstream}) => workstream),
    },
    correctness: {
      source: {kind: 'run-log'},
      scope,
      freshness,
      availability: availability(correctnessReasons),
      values: state.correctnessGates.map(({startProof: _startProof, ...gate}) => gate),
    },
    unprojectedSpans: unprojectedSpans(state),
  };
}

function projectionTruncation(state: RunDetailState): RunDetailProjection['truncation'] {
  return {
    agentEvictions: state.truncation.agentEvictions,
    correctnessGateEvictions: state.truncation.correctnessGateEvictions,
    epochEvictions: state.truncation.epochEvictions,
    issueEvictions: state.truncation.issueEvictions,
    metricSeriesEvictions: state.truncation.metricSeriesEvictions,
    workstreamEvictions: state.truncation.workstreamEvictions,
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

function unprojectedSpans(state: RunDetailState): ProjectionSet<UnprojectedSpanProjection> {
  const scope = {kind: 'run' as const, runId: state.runId};
  return {
    source: {kind: 'run-log'},
    scope,
    freshness: freshnessOf(state),
    availability:
      state.truncation.issueEvictions > 0
        ? {kind: 'partial', reasons: ['state-truncated']}
        : {kind: 'available'},
    values: state.issues.map(issue => ({
      spanId: issue.spanId,
      kind: issue.kind,
      attrsType: issue.attrsType,
      declaredAttrsType: issue.declaredAttrsType,
      reason: issue.reason,
      source: {kind: 'run-record', sequence: issue.sequence, epoch: issue.epoch},
      scope: {kind: 'span', runId: state.runId, spanId: issue.spanId},
    })),
  };
}

function spanReasons(
  state: RunDetailState,
  attrsType: string,
  familyEvictions: number,
): ProjectionReason[] {
  const issues = state.issues.filter(
    issue => (issue.declaredAttrsType ?? issue.attrsType) === attrsType,
  );
  const reasons: ProjectionReason[] = [];
  if (issues.some(issue => issue.reason === 'undeclared-kind')) {
    reasons.push('declaration-unknown');
  }
  if (issues.some(issue => issue.reason === 'descriptor-unknown')) {
    reasons.push('descriptor-unknown');
  }
  if (
    issues.some(
      issue =>
        issue.reason === 'missing-span-start' ||
        issue.reason === 'descriptor-mismatch' ||
        issue.reason === 'invalid-attributes',
    )
  ) {
    reasons.push('invalid-typed-fact');
  }
  if (
    familyEvictions > 0 ||
    state.truncation.epochEvictions > 0 ||
    state.truncation.issueEvictions > 0
  ) {
    reasons.push('state-truncated');
  }
  return reasons;
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
