import {useEffect, useMemo, useState} from 'react';
import type {CampaignRecord} from './campaign-record.js';
import type {CampaignView, CampaignViewModel} from './campaign-view.js';

function latestMeasurementIndex(
  measurements: CampaignRecord['measurements'],
  metricId?: string,
): number {
  for (let index = measurements.length - 1; index >= 0; index--) {
    if (
      metricId === undefined ||
      measurements[index]?.values.some(value => value.metricId === metricId)
    )
      return index;
  }
  return 0;
}

function timelineBounds(scenario: CampaignRecord): {start: number; end: number} {
  const start = Math.min(...scenario.workstreams.map(item => Date.parse(item.startedAt)));
  const end = Math.max(...scenario.workstreams.map(item => Date.parse(item.finishedAt)));
  return {start: Number.isFinite(start) ? start : 0, end: Number.isFinite(end) ? end : 1};
}

export function useCampaignHistory(scenario: CampaignRecord): CampaignViewModel {
  const [view, setView] = useState<CampaignView>('dashboard');
  const orderedMeasurements = useMemo(
    () => [...scenario.measurements].sort((a, b) => a.sequence - b.sequence),
    [scenario.measurements],
  );
  const initialMetric =
    orderedMeasurements.at(-1)?.values[0]?.metricId ??
    scenario.objective.target?.metricId ??
    scenario.objective.metrics[0]?.id ??
    '';
  const [pointIndex, setPointIndex] = useState(
    latestMeasurementIndex(orderedMeasurements, initialMetric),
  );
  const [selectedWorkstreamId, setSelectedWorkstreamId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [metricId, setMetricId] = useState(initialMetric);
  const activeMeasurement = orderedMeasurements[pointIndex];
  const metric =
    scenario.objective.metrics.find(item => item.id === metricId) ?? scenario.objective.metrics[0];
  const selectedWorkstream =
    scenario.workstreams.find(item => item.id === selectedWorkstreamId) ?? null;
  const selectedAgent = scenario.agents.find(item => item.id === selectedAgentId) ?? null;
  const timeline = useMemo(() => timelineBounds(scenario), [scenario]);
  const latestTimestamp = activeMeasurement?.timestamp ?? new Date(timeline.end).toISOString();
  const cursorSequence = activeMeasurement?.sequence ?? Number.POSITIVE_INFINITY;
  const visibleWorkstreamCount = scenario.workstreams.filter(
    item => Date.parse(item.startedAt) <= Date.parse(latestTimestamp),
  ).length;
  const setMetric = (nextMetricId: string): void => {
    setMetricId(nextMetricId);
    setPointIndex(latestMeasurementIndex(orderedMeasurements, nextMetricId));
    setPlaying(false);
  };

  useEffect(() => {
    if (!playing) return;
    const timer = window.setInterval(() => {
      setPointIndex(index => Math.min(index + 1, orderedMeasurements.length - 1));
    }, 300);
    return () => window.clearInterval(timer);
  }, [orderedMeasurements.length, playing]);

  useEffect(() => {
    if (playing && pointIndex >= orderedMeasurements.length - 1) setPlaying(false);
  }, [orderedMeasurements.length, playing, pointIndex]);

  useEffect(() => {
    if (
      activeMeasurement !== undefined &&
      !activeMeasurement.values.some(value => value.metricId === metricId)
    ) {
      setMetricId(activeMeasurement.values[0]?.metricId ?? metricId);
    }
  }, [activeMeasurement, metricId]);

  const showMeasurement = (index: number): void => {
    setPointIndex(Math.max(0, Math.min(orderedMeasurements.length - 1, index)));
  };

  return {
    activeMeasurement,
    cursorSequence,
    latestTimestamp,
    metric,
    metricId,
    orderedMeasurements,
    playing,
    pointIndex,
    selectedAgent,
    selectedWorkstream,
    setMetric,
    setPlaying,
    setPointIndex: showMeasurement,
    setSelectedAgentId,
    setSelectedWorkstreamId,
    setView,
    status: pointIndex < orderedMeasurements.length - 1 ? 'active' : 'completed',
    timeline,
    view,
    visibleWorkstreamCount,
  };
}
