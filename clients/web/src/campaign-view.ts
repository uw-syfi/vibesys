import type {CampaignRecord} from './campaign-record.js';

export type CampaignView = 'dashboard' | 'objective';

/**
 * Source-neutral state and actions consumed by the campaign UI.
 * A fixture clock or a live event fold may implement this interface.
 */
export interface CampaignViewModel {
  readonly activeMeasurement: CampaignRecord['measurements'][number] | undefined;
  readonly cursorSequence: number;
  readonly latestTimestamp: string;
  readonly metric: CampaignRecord['objective']['metrics'][number] | undefined;
  readonly metricId: string;
  readonly orderedMeasurements: CampaignRecord['measurements'];
  readonly playing: boolean;
  readonly pointIndex: number;
  readonly selectedAgent: CampaignRecord['agents'][number] | null;
  readonly selectedWorkstream: CampaignRecord['workstreams'][number] | null;
  readonly status: 'active' | 'completed';
  readonly timeline: {readonly start: number; readonly end: number};
  readonly view: CampaignView;
  readonly visibleWorkstreamCount: number;
  readonly setMetric: (metricId: string) => void;
  readonly setPlaying: (playing: boolean) => void;
  readonly setPointIndex: (index: number) => void;
  readonly setSelectedAgentId: (id: string | null) => void;
  readonly setSelectedWorkstreamId: (id: string | null) => void;
  readonly setView: (view: CampaignView) => void;
}
