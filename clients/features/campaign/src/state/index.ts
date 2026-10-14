/** Campaign records and their pure frame projection; no browser or transport implementation. */

export {
  type CampaignAction,
  type CampaignContext,
  type CampaignInput,
  campaignMachine,
  campaignRecord,
  initialCampaignUi,
  orderedMeasurements,
} from './campaign.js';
export {
  type CampaignIncumbent,
  type CampaignMetricAnalytics,
  projectCampaignMetric,
} from './campaign-analytics.js';
export {
  type CampaignFoldError,
  type CampaignFoldOutcome,
  campaignFoldErrorMessage,
  type FoldState,
  foldCampaignFrame,
  foldFrames,
  foldStateToRecord,
  initialFoldState,
  latestMoment,
} from './campaign-fold.js';
export {
  type CampaignFrame,
  type CampaignHeader,
  type FrameAgent,
  type FrameMeasurement,
  type FrameWorkstream,
  isTerminalPhase,
  parseCampaignFrame,
  type WorkstreamPhase,
} from './campaign-frames.js';
export {type CampaignReadonlyView, projectCampaign} from './campaign-projection.js';
export type {CampaignRecord, TurnMessage, WorkstreamOutcome} from './campaign-record.js';
export {framesFromRecord} from './campaign-replay.js';
export type {CampaignStream, FrameListener, StreamErrorListener} from './campaign-stream.js';
export type {
  CampaignTimeline,
  CampaignView,
  CampaignViewModel,
  WorkstreamLayout,
  WorkstreamSort,
} from './campaign-view.js';
export {
  type MeasurementDisposition,
  type MetricDirection,
  parseReplayScenario,
  type ReplayMetricDefinition,
  type ReplayScenario,
} from './replay-scenario.js';
