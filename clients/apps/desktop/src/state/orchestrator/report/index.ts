/** The run report's view models, as plain reads of the folded state. */
export {type AgentsStrip, type AgentTypeCount, agentsStrip} from './agents-strip.js';
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
export {timeTicks, valueTicks} from './ticks.js';
export {type TradeoffPointView, type TradeoffReport, tradeoffReport} from './tradeoff.js';
