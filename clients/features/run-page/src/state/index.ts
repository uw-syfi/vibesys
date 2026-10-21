/**
 * The Run screen's pure state: its store (`runPageStep`, a reducer with request tokens), the run
 * at the view (`createRunReader`), the zoom (`zoomMachine` over a `Route`), and the selectors the
 * screen renders: header, stop, quota and ended banners, the startup timeline, the run level's
 * hero, chart, ideas and Now, the logs, and the scrubber (`scrubberView`) over the trace log.
 */

export type {UsageRecord} from '@vibesys/contracts/host-helper';
export {ideaOfAttempt} from './attempt-ideas.js';
export {type BaselineColumns, baselineColumns} from './baselines.js';
export {
  type GatewayEvent,
  type GatewayInput,
  type GatewayLogInput,
  type GatewayRunContext,
  gatewayRunMachine,
} from './gateway-machine.js';
export {type HistoryLoadingView, historyLoadingView} from './history-words.js';
export {type IdeaBase, type IdeaBases, ideaBases} from './idea-bases.js';
export {effectWords, type IdeaEffect, type IdeaEffects, ideaEffects} from './idea-effects.js';
export {type IdeaFocusView, ideaFocusView, ideaOutcomeLine, openIdeaId} from './idea-focus.js';
export {
  type ImplementerOutcome,
  type ImplementerOutcomes,
  implementerOutcomes,
} from './idea-outcomes.js';
export {outcomeWords} from './idea-words.js';
export {
  type BestLine,
  bestLine,
  defaultLayout,
  type IdeasLayout,
  type IdeaTree,
  type IdeaTreeEntry,
  ideaAncestry,
  ideaTree,
} from './ideas-tree.js';
export {
  ageWords,
  chipPressed,
  type IdeaRow,
  type IdeasFilter,
  ideasView,
  noIdeasWords,
  pressChip,
} from './ideas-view.js';
export type {GateFailure} from './input-gates.js';
export {
  EMPTY_TRACE_LOG,
  isTraceEventType,
  type RunData,
  type SessionView,
  type TraceLog,
} from './live.js';
export {type ModelNames, modelNamesOf, NO_MODEL_NAMES} from './model-names.js';
export {roleLabel, stepKicker, stepLabel, stepTitle} from './names.js';
export {
  objectivesOf,
  type ParetoAxisView,
  type ParetoPointView,
  type ParetoView,
  paretoView,
} from './pareto-view.js';
export {predatesTraceWords, roundsBeforeTrace} from './predates-trace.js';
export {pauseWords, workedWords} from './quota-pauses.js';
export {type ReaderReply, readerHeld} from './reader.js';
export {runRecord, sameRecord} from './record.js';
export {roleSeries} from './role-series.js';
export {createFactsReader} from './run-facts.js';
export {
  clockAt,
  clockStart,
  type HeroView,
  heroView,
  logStreamToFollow,
  type MetricTileView,
  type NowItem,
  nowView,
  type ResolvedZoom,
  resolveZoom,
  stripChartView,
} from './run-level.js';
export {createRunReader, type RunModel} from './run-model.js';
export {
  type HistoryProgress,
  type HistoryReply,
  type ResumeWhen,
  type RunPageAction,
  type RunPageRequest,
  type RunPageSeed,
  type RunPageState,
  type RunRecord,
  type RunView,
  runHasEnded,
  runNow,
  runPageInitial,
  runPageStep,
  runPhase,
  runWallNow,
  type SpanDetailReply,
} from './run-page.js';
export {type SummaryReply, summaryFacts, type WireSummary} from './run-summary.js';
export {scrubberView, viewAtPosition} from './scrub.js';
export {projectSkeleton, skeletonPositions, skeletonScreenState} from './skeleton-projection.js';
export {spendOf, tokensByAgent, turnTokens} from './token-usage.js';
export {
  type TokensCell,
  type TokensCells,
  type TokensReading,
  type TurnSpend,
  tokensCells,
  tokensColumn,
  tokensLine,
  tokensReading,
  tokensTitle,
} from './token-words.js';
export {
  followed,
  groupedBy,
  groupedWithin,
  roleAgents,
  TOKEN_KINDS,
  type TokenKind,
  USAGE_CHARTS,
  USAGE_CONTROLS_DEFAULT,
  USAGE_GROUPS,
  USAGE_METRICS,
  type UsageChart,
  type UsageContext,
  type UsageControls,
  type UsageGroupBy,
  type UsageLabel,
  type UsageMetric,
  type UsageRowView,
  type UsageSlice,
  type UsageTile,
  type UsageView,
  unfolded,
  usageContext,
  usageView,
  withKind,
} from './usage-view.js';
export {
  agentWho,
  type Who,
  type WhoBySpan,
  whoBySpan,
  whoShort,
  whoWords,
} from './usage-who.js';
export {
  endedView,
  followedStreams,
  headerView,
  historyLoading,
  historyProblem,
  machineBlockWords,
  type Offer,
  pauseNowBannerView,
  quotaBannerView,
  runEnding,
  runGoneWords,
  streamLines,
  type TraceArrival,
  timelineView,
  traceArrival,
  transcriptLoading,
  unplacedRun,
  usageRecords,
  viewedRun,
} from './views.js';
export {
  type Route,
  RUN_ROUTE,
  type RunMode,
  routeFromQuery,
  routeLevel,
  routeQuery,
  type ZoomEvent,
  zoomMachine,
} from './zoom.js';
