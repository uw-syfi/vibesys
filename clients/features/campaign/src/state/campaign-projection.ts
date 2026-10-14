/** Pure projection shared by fixture playback and a live frame stream. */

import {type CampaignContext, campaignRecord, orderedMeasurements} from './campaign.js';
import {latestMoment} from './campaign-fold.js';
import type {CampaignRecord} from './campaign-record.js';
import type {CampaignViewModel} from './campaign-view.js';

export type CampaignReadonlyView = Omit<
  CampaignViewModel,
  | 'setMetric'
  | 'setPlaying'
  | 'setPointIndex'
  | 'setSelectedAgentId'
  | 'setSelectedWorkstreamId'
  | 'setView'
  | 'setTimelineMode'
  | 'setWorkstreamLayout'
  | 'setWorkstreamSort'
>;

function timelineBounds(record: CampaignRecord): {start: number; end: number} {
  const start = Math.min(...record.workstreams.map(item => Date.parse(item.startedAt)));
  const end = Math.max(...record.workstreams.map(item => Date.parse(item.finishedAt)));
  return {start: Number.isFinite(start) ? start : 0, end: Number.isFinite(end) ? end : 1};
}

function measurementMetric(
  measurement: CampaignRecord['measurements'][number] | undefined,
  requested: string,
): string {
  if (measurement === undefined || measurement.values.some(value => value.metricId === requested))
    return requested;
  return measurement.values[0]?.metricId ?? requested;
}

export function projectCampaign(context: CampaignContext): {
  scenario: CampaignRecord | null;
  campaign: CampaignReadonlyView | null;
} {
  const scenario = campaignRecord(context);
  if (scenario === null) return {scenario, campaign: null};
  const ui = context.ui;
  const measurements = orderedMeasurements(scenario);
  const tail = Math.max(0, measurements.length - 1);
  const pointIndex = context.mode === 'live' && ui.following ? tail : Math.min(ui.pointIndex, tail);
  const activeMeasurement = measurements[pointIndex];
  const metricId = measurementMetric(activeMeasurement, ui.metricId);
  const timeline = timelineBounds(scenario);
  const latestTimestamp =
    context.mode === 'live' && ui.following
      ? new Date(latestMoment(context.fold)).toISOString()
      : (activeMeasurement?.timestamp ?? new Date(timeline.end).toISOString());
  return {
    scenario,
    campaign: {
      activeMeasurement,
      cursorSequence: activeMeasurement?.sequence ?? Number.POSITIVE_INFINITY,
      latestTimestamp,
      metric:
        scenario.objective.metrics.find(item => item.id === metricId) ??
        scenario.objective.metrics[0],
      metricId,
      orderedMeasurements: measurements,
      playing: context.mode === 'live' ? ui.following : ui.playing,
      pointIndex,
      selectedAgent: scenario.agents.find(item => item.id === ui.selectedAgentId) ?? null,
      selectedWorkstream:
        scenario.workstreams.find(item => item.id === ui.selectedWorkstreamId) ?? null,
      status:
        context.mode === 'live' ? context.fold.status : pointIndex < tail ? 'active' : 'completed',
      timeline,
      timelineMode: ui.timelineMode,
      view: ui.view,
      visibleWorkstreamCount: scenario.workstreams.filter(
        item => Date.parse(item.startedAt) <= Date.parse(latestTimestamp),
      ).length,
      workstreamLayout: ui.workstreamLayout,
      workstreamSort: ui.workstreamSort,
      workstreamTokenSpend:
        context.mode === 'live' ? Object.fromEntries(context.fold.tokenSpend) : {},
    },
  };
}
