import type {
  FrameOperatorAction,
  WorkstreamActivityState,
  WorkstreamAttemptState,
} from './campaign-frames.js';
import type {CampaignRecord} from './campaign-record.js';

export type CampaignView = 'dashboard' | 'objective';
/** Whether the view model's source is receiving frames promptly. A fixture
 * clock has no notion of staleness and always reports `fresh`; a live fold
 * derives it from the time since the last frame arrived. */
export type ConnectionFreshness = 'fresh' | 'stale';
export type CampaignTimeline = 'workstreams' | 'agents';
export type WorkstreamLayout = 'kanban' | 'table';
export type WorkstreamSort =
  | 'start-asc'
  | 'start-desc'
  | 'end-asc'
  | 'end-desc'
  | 'duration-desc'
  | 'duration-asc'
  | 'tokens-desc'
  | 'tokens-asc';

/**
 * Source-neutral state and actions consumed by the campaign UI.
 * A fixture clock or a live event fold may implement this interface.
 */
export interface CampaignViewModel {
  readonly activeMeasurement: CampaignRecord['measurements'][number] | undefined;
  readonly connectionFreshness: ConnectionFreshness;
  readonly cursorSequence: number;
  readonly latestTimestamp: string;
  readonly metric: CampaignRecord['objective']['metrics'][number] | undefined;
  readonly metricId: string;
  readonly operatorActions: readonly FrameOperatorAction[];
  readonly orderedMeasurements: CampaignRecord['measurements'];
  readonly playing: boolean;
  readonly pointIndex: number;
  readonly selectedAgent: CampaignRecord['agents'][number] | null;
  readonly selectedWorkstream: CampaignRecord['workstreams'][number] | null;
  readonly status: 'active' | 'completed';
  readonly timeline: {readonly start: number; readonly end: number};
  readonly timelineMode: CampaignTimeline;
  readonly view: CampaignView;
  readonly visibleWorkstreamCount: number;
  readonly workstreamActivity: Readonly<Record<string, WorkstreamActivityState>>;
  readonly workstreamAttempts: Readonly<Record<string, WorkstreamAttemptState>>;
  readonly workstreamLayout: WorkstreamLayout;
  readonly workstreamSort: WorkstreamSort;
  readonly workstreamTokenSpend: Readonly<Record<string, number>>;
  readonly setMetric: (metricId: string) => void;
  readonly setPlaying: (playing: boolean) => void;
  readonly setPointIndex: (index: number) => void;
  readonly setSelectedAgentId: (id: string | null) => void;
  readonly setSelectedWorkstreamId: (id: string | null) => void;
  readonly setView: (view: CampaignView) => void;
  readonly setTimelineMode: (mode: CampaignTimeline) => void;
  readonly setWorkstreamLayout: (layout: WorkstreamLayout) => void;
  readonly setWorkstreamSort: (sort: WorkstreamSort) => void;
}
