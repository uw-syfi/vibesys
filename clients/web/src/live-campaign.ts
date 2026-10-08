/**
 * The live campaign view model: fold streamed frames into a `CampaignRecord`
 * and present it to the existing dashboard, auto-following the tail.
 *
 * This is the live counterpart to `useCampaignHistory`. The difference is where
 * the record comes from (a frame fold, not a loaded fixture) and two semantics
 * that only make sense live: the cursor follows the newest measurement until
 * the user scrubs, and `status` is the folded run status rather than a function
 * of the cursor. The fold itself is pure (`campaign-fold.ts`); this hook is the
 * React shell that owns the subscription.
 */
import {useEffect, useMemo, useState} from 'react';
import {
  type FoldState,
  foldCampaignFrame,
  foldStateToRecord,
  initialFoldState,
  latestMoment,
} from './campaign-fold.js';
import type {CampaignRecord} from './campaign-record.js';
import type {CampaignStream} from './campaign-stream.js';
import type {
  CampaignTimeline,
  CampaignView,
  CampaignViewModel,
  WorkstreamLayout,
  WorkstreamSort,
} from './campaign-view.js';

export interface LiveCampaignState {
  /** The folded record, or null before the campaign header has arrived. */
  readonly scenario: CampaignRecord | null;
  readonly campaign: CampaignViewModel;
  readonly streamError: Error | null;
}

/** A structurally valid empty record used only to build a view model before the
 * header arrives; callers gate rendering on `scenario !== null`. */
const EMPTY_RECORD: CampaignRecord = {
  schemaVersion: 1,
  id: '',
  title: '',
  summary: '',
  provenance: '',
  objective: {title: '', statement: '', target: null, constraints: [], gates: [], metrics: []},
  benchmarkVersions: [],
  benchmarkVersionBoundary: {fromVersion: '', toVersion: '', afterSequence: 0, reason: ''},
  workstreams: [],
  agents: [],
  measurements: [],
  trajectories: [],
};

function timelineBounds(record: CampaignRecord): {start: number; end: number} {
  const start = Math.min(...record.workstreams.map(item => Date.parse(item.startedAt)));
  const end = Math.max(...record.workstreams.map(item => Date.parse(item.finishedAt)));
  return {start: Number.isFinite(start) ? start : 0, end: Number.isFinite(end) ? end : 1};
}

function tokenSpendObject(fold: FoldState): Readonly<Record<string, number>> {
  return Object.fromEntries(fold.tokenSpend);
}

/** The read-only half of the view model (everything but the setters). */
type CampaignViewReadonly = Omit<
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

interface LiveCursor {
  readonly pointIndex: number;
  readonly following: boolean;
  readonly tailIndex: number;
}

/** Resolve the cursor: follow the tail unless a scrub index pins it. */
function liveCursor(count: number, scrubIndex: number | null): LiveCursor {
  const tailIndex = Math.max(0, count - 1);
  const following = scrubIndex === null;
  return {
    tailIndex,
    following,
    pointIndex: following ? tailIndex : Math.min(scrubIndex, tailIndex),
  };
}

interface LiveUiState {
  readonly metricId: string;
  readonly view: CampaignView;
  readonly timelineMode: CampaignTimeline;
  readonly workstreamLayout: WorkstreamLayout;
  readonly workstreamSort: WorkstreamSort;
  readonly selectedWorkstreamId: string | null;
  readonly selectedAgentId: string | null;
}

interface ReadonlyViewInputs {
  readonly source: CampaignRecord;
  readonly status: 'active' | 'completed';
  readonly orderedMeasurements: CampaignRecord['measurements'];
  readonly timeline: {readonly start: number; readonly end: number};
  readonly workstreamTokenSpend: Readonly<Record<string, number>>;
  readonly cursor: LiveCursor;
  readonly ui: LiveUiState;
}

/** Project the folded record and UI state into the dashboard's read-only
 * contract. Status comes from the fold, not the cursor, so scrubbing never
 * rewrites the run status. */
function readOnlyView(inputs: ReadonlyViewInputs): CampaignViewReadonly {
  const {source, status, orderedMeasurements, timeline, workstreamTokenSpend, cursor, ui} = inputs;
  const activeMeasurement = orderedMeasurements[cursor.pointIndex];
  const latestTimestamp = activeMeasurement?.timestamp ?? new Date(timeline.end).toISOString();
  return {
    activeMeasurement,
    cursorSequence: activeMeasurement?.sequence ?? Number.POSITIVE_INFINITY,
    latestTimestamp,
    metric:
      source.objective.metrics.find(item => item.id === ui.metricId) ?? source.objective.metrics[0],
    metricId: ui.metricId,
    orderedMeasurements,
    playing: cursor.following,
    pointIndex: cursor.pointIndex,
    selectedAgent: source.agents.find(item => item.id === ui.selectedAgentId) ?? null,
    selectedWorkstream:
      source.workstreams.find(item => item.id === ui.selectedWorkstreamId) ?? null,
    status,
    timeline,
    timelineMode: ui.timelineMode,
    view: ui.view,
    visibleWorkstreamCount: source.workstreams.filter(
      item => Date.parse(item.startedAt) <= Date.parse(latestTimestamp),
    ).length,
    workstreamLayout: ui.workstreamLayout,
    workstreamSort: ui.workstreamSort,
    workstreamTokenSpend,
  };
}

export function useLiveCampaign(stream: CampaignStream): LiveCampaignState {
  const [fold, setFold] = useState<FoldState>(initialFoldState);
  const [streamError, setStreamError] = useState<Error | null>(null);
  // null means "follow the tail"; a number pins the cursor to a scrubbed point.
  const [scrubIndex, setScrubIndex] = useState<number | null>(null);
  const [view, setView] = useState<CampaignView>('dashboard');
  const [metricId, setMetricId] = useState('');
  const [selectedWorkstreamId, setSelectedWorkstreamId] = useState<string | null>(null);
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);
  const [workstreamLayout, setWorkstreamLayout] = useState<WorkstreamLayout>('kanban');
  const [workstreamSort, setWorkstreamSort] = useState<WorkstreamSort>('start-asc');
  const [timelineMode, setTimelineMode] = useState<CampaignTimeline>('workstreams');

  useEffect(() => {
    setFold(initialFoldState());
    setStreamError(null);
    return stream.subscribe(
      frame => setFold(current => foldCampaignFrame(current, frame)),
      error => setStreamError(error),
    );
  }, [stream]);

  const scenario = useMemo(() => foldStateToRecord(fold, latestMoment(fold)), [fold]);
  const source = scenario ?? EMPTY_RECORD;
  const orderedMeasurements = useMemo(
    () => [...source.measurements].sort((a, b) => a.sequence - b.sequence),
    [source.measurements],
  );
  const workstreamTokenSpend = useMemo(() => tokenSpendObject(fold), [fold]);
  const timeline = useMemo(() => timelineBounds(source), [source]);
  const cursor = liveCursor(orderedMeasurements.length, scrubIndex);
  const readOnly = readOnlyView({
    source,
    status: fold.status,
    orderedMeasurements,
    timeline,
    workstreamTokenSpend,
    cursor,
    ui: {
      metricId,
      view,
      timelineMode,
      workstreamLayout,
      workstreamSort,
      selectedWorkstreamId,
      selectedAgentId,
    },
  });

  // Keep the selected metric on one the current measurement reports, including
  // the first measurement after an empty start.
  useEffect(() => {
    const active = readOnly.activeMeasurement;
    if (active !== undefined && !active.values.some(value => value.metricId === metricId))
      setMetricId(active.values[0]?.metricId ?? metricId);
  }, [readOnly.activeMeasurement, metricId]);

  const setPointIndex = (index: number): void => {
    const clamped = Math.max(0, Math.min(cursor.tailIndex, index));
    setScrubIndex(clamped >= cursor.tailIndex ? null : clamped);
  };
  const setPlaying = (playing: boolean): void => setScrubIndex(playing ? null : cursor.pointIndex);
  const setMetric = (nextMetricId: string): void => setMetricId(nextMetricId);

  return {
    scenario,
    streamError,
    campaign: {
      ...readOnly,
      setMetric,
      setPlaying,
      setPointIndex,
      setSelectedAgentId,
      setSelectedWorkstreamId,
      setView,
      setTimelineMode,
      setWorkstreamLayout,
      setWorkstreamSort,
    },
  };
}
