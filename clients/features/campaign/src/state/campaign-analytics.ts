/** Accepted campaign incumbents projected independently from the observation series. */

import type {CampaignRecord, MetricDirection} from './campaign-record.js';

export interface CampaignIncumbent {
  readonly measurementId: string;
  readonly measurementIndex: number;
  readonly sequence: number;
  readonly value: number;
}

export interface CampaignMetricAnalytics {
  readonly incumbent: CampaignIncumbent | null;
  readonly runningBestSteps: readonly CampaignIncumbent[];
}

function isBetter(candidate: number, incumbent: number, direction: MetricDirection): boolean {
  return direction === 'minimize' ? candidate < incumbent : candidate > incumbent;
}

/**
 * Project the accepted incumbent for one metric through a historical point index.
 *
 * Every measurement remains part of the caller's observation series. This
 * projection contains only accepted measurements that establish or improve
 * the incumbent, so presentation consumers cannot reinterpret gate outcomes.
 */
export function projectCampaignMetric(
  record: CampaignRecord,
  metricId: string,
  throughIndex: number,
): CampaignMetricAnalytics {
  const direction = record.objective.metrics.find(metric => metric.id === metricId)?.direction;
  if (direction === undefined) return {incumbent: null, runningBestSteps: []};

  let incumbent: CampaignIncumbent | null = null;
  const runningBestSteps: CampaignIncumbent[] = [];
  const measurements = [...record.measurements].sort(
    (left, right) => left.sequence - right.sequence,
  );
  for (const [measurementIndex, measurement] of measurements.entries()) {
    if (measurementIndex > throughIndex || measurement.disposition !== 'accepted') continue;
    const value = measurement.values.find(item => item.metricId === metricId)?.value;
    if (value === undefined || (incumbent !== null && !isBetter(value, incumbent.value, direction)))
      continue;
    incumbent = {
      measurementId: measurement.id,
      measurementIndex,
      sequence: measurement.sequence,
      value,
    };
    runningBestSteps.push(incumbent);
  }
  return {incumbent, runningBestSteps};
}
