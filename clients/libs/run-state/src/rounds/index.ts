/**
 * `@vibesys/run-state/rounds`: round identity and the round projections over the run fold: each
 * round's agent phases and timing, planned rounds, outcomes, experiment ownership, and the run's
 * active focus. Frontends read these instead of parsing backend labels.
 */
export {type RoundKey, roundKeyFor, sameRoundKey} from '../round-key.js';
export {hasActiveAgentTiming} from '../round-timing.js';
export {type AgentPhase, phasesForRound, type RoundState, roundAgentElapsedMs} from '../run-map.js';
export {
  activeRunFocus,
  agentKindText,
  describePhase,
  phaseText,
  planningStageForPhase,
} from './phase-semantics.js';
export {
  experimentForRound,
  hypothesisRoundNumbers,
  joinRoundsWithExperiments,
  type RoundOutcome,
  roundOutcome,
  roundsWithPlan,
} from './round-projection.js';
