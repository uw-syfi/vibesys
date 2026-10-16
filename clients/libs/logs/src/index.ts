/**
 * Following a run's log streams through the host helper's `logs` feature
 * (`docs/contributing/desktop-connection.md`, "Logs"): `foldChunk` folds `logs.read` chunks into a
 * bounded line view (the data fold), `logFollowMachine` reads a stream's tail and then follows it
 * with one read in flight (the control state), and `startLogFollower` runs that machine over a
 * `LogTransport` the composition provides. Neutral: no DOM, no Node.
 */

export {type EscapeState, stripEscapes} from './escapes.js';
export {
  DEFAULT_LOG_FOLLOW_CONFIG,
  type LogFollowConfig,
  type LogFollowContext,
  type LogFollowEvent,
  type LogFollowInput,
  type LogReadFailure,
  type LogReadFailureCode,
  type LogReadInput,
  type LogReadOutcome,
  logFollowMachine,
  logReadRetryable,
} from './follow.js';
export {
  type LogFollower,
  type LogTransport,
  logFollowMachineFor,
  startLogFollower,
} from './follower.js';
export {decodeNarrationLine, type NarrationRecord} from './narration.js';
export {type LogListOutcome, logsUnsupported, type RunLogSource} from './source.js';
export {
  DEFAULT_LOG_VIEW_BOUNDS,
  emptyLogView,
  foldChunk,
  type LogLine,
  type LogView,
  type LogViewBounds,
  logViewComplete,
  logViewLines,
  logViewRecords,
  type PipeRun,
  type PipeSegment,
  pipeSegments,
} from './view.js';
