/** The run report's view models, as plain reads of the folded state. */
export {type AgentCostRow, agentCostRows} from './agent-costs.js';
export {type AgentsStrip, type AgentTypeCount, agentsStrip} from './agents-strip.js';
export {type RunFacts, runFacts} from './facts.js';
export {
  type GainView,
  type IdeaRow,
  type IdeaSortKey,
  type IdeasReport,
  type IdeaWindow,
  ideasInRun,
  ideasInWindow,
  ideasMatching,
  ideasOfVerdict,
  nextSort,
  type SortDirection,
  sortIdeas,
  VERDICT_ORDER,
  type Verdict,
  verdictCounts,
  type WorkstreamRow,
} from './ideas.js';
export {
  LATEST_COUNT,
  type LatestMeasurement,
  type LatestValue,
  latestMeasurements,
} from './latest.js';
export {
  type Better,
  betterOf,
  type MeasuredPointView,
  type ObjectiveReport,
  objectiveReports,
} from './objectives.js';
export {
  type ReportHeader,
  type RunReport,
  runReport,
  type SearchReading,
} from './run-report.js';
export {type SummaryField, type SummaryView, summaryReport} from './summary.js';
export {valueTicks} from './ticks.js';
export {type TradeoffPointView, type TradeoffReport, tradeoffReport} from './tradeoff.js';
export {
  type BandView,
  bandView,
  boundIsDrawn,
  choiceOf,
  LONG_RUN_MS,
  offersRanges,
  type RangeChoice,
  type RangeView,
  rangeChoices,
  type Span,
  WHOLE_RUN,
  windowOf,
} from './windows.js';
