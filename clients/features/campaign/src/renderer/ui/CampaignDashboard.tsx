import {
  type CSSProperties,
  type JSX,
  type KeyboardEvent,
  type ReactNode,
  useRef,
  useState,
} from 'react';
import type {CampaignRecord, TurnMessage} from '../../state/campaign-record.js';
import type {CampaignViewModel} from '../../state/campaign-view.js';
import brandMark from './assets/brand-mark.svg';
import {CampaignTimeline} from './CampaignTimeline.js';
import {Button} from './components/button.js';
import {Input} from './components/input.js';
import {NativeSelect, NativeSelectOption} from './components/native-select.js';
import {Sheet, SheetClose, SheetContent, SheetTitle, SheetTrigger} from './components/sheet.js';

interface CampaignDashboardProps {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
  readonly headerAction?: ReactNode;
  readonly previewLabel?: 'Preview' | 'Stream preview';
}

const CHART = {left: 58, right: 912, top: 26, bottom: 258};
const ROLE_COLORS = [
  'var(--role-1)',
  'var(--role-2)',
  'var(--role-3)',
  'var(--role-4)',
  'var(--role-5)',
  'var(--role-6)',
];

export function CampaignDashboard({
  scenario,
  campaign,
  headerAction,
  previewLabel,
}: CampaignDashboardProps): JSX.Element {
  const [agentReturnWorkstreamId, setAgentReturnWorkstreamId] = useState<string | null>(null);
  const workstreamReturnFocus = useRef<HTMLElement | null>(null);
  return (
    <main className="campaign-shell">
      <CampaignHeader
        scenario={scenario}
        campaign={campaign}
        headerAction={headerAction}
        previewLabel={previewLabel}
      />
      <CampaignOverview scenario={scenario} campaign={campaign} />
      {campaign.selectedWorkstream !== null && (
        <WorkstreamDialog
          scenario={scenario}
          workstream={campaign.selectedWorkstream}
          cursorSequence={campaign.cursorSequence}
          cursorTimestamp={campaign.latestTimestamp}
          returnFocus={workstreamReturnFocus.current}
          onClose={() => {
            campaign.setSelectedWorkstreamId(null);
            workstreamReturnFocus.current = null;
          }}
          onAgent={(id, opener) => {
            workstreamReturnFocus.current = opener;
            setAgentReturnWorkstreamId(campaign.selectedWorkstream?.id ?? null);
            campaign.setSelectedWorkstreamId(null);
            campaign.setSelectedAgentId(id);
          }}
        />
      )}
      {campaign.selectedAgent !== null && (
        <AgentDialog
          scenario={scenario}
          agentId={campaign.selectedAgent.id}
          cursorSequence={campaign.cursorSequence}
          cursorTimestamp={campaign.latestTimestamp}
          onClose={() => {
            campaign.setSelectedAgentId(null);
            setAgentReturnWorkstreamId(null);
            workstreamReturnFocus.current = null;
          }}
          onBack={
            agentReturnWorkstreamId === null
              ? undefined
              : () => {
                  campaign.setSelectedAgentId(null);
                  campaign.setSelectedWorkstreamId(agentReturnWorkstreamId);
                  setAgentReturnWorkstreamId(null);
                }
          }
          onWorkstream={id => {
            campaign.setSelectedAgentId(null);
            campaign.setSelectedWorkstreamId(id);
            setAgentReturnWorkstreamId(null);
            workstreamReturnFocus.current = null;
          }}
        />
      )}
    </main>
  );
}

function CampaignHeader({
  scenario,
  campaign,
  headerAction,
  previewLabel,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
  readonly headerAction?: ReactNode;
  readonly previewLabel: 'Preview' | 'Stream preview' | undefined;
}): JSX.Element {
  const best = bestMeasurement(scenario, campaign.metricId, campaign.cursorSequence)?.values.find(
    value => value.metricId === campaign.metricId,
  )?.value;
  return (
    <>
      <header className="campaign-header">
        <div className="brand-lockup">
          <img className="brand-mark" src={brandMark} alt="" aria-hidden="true" />
          <span>VIBESYS</span>
        </div>
        <div className="campaign-header-actions">
          <div className="run-state">
            {previewLabel === undefined ? (
              <>
                <span className="live-dot" /> {campaign.status}
              </>
            ) : (
              <span className="run-preview-status">
                {previewLabel} · {campaign.status}
              </span>
            )}
          </div>
          {headerAction}
        </div>
      </header>
      <div className="run-overview">
        <section className="campaign-intro" aria-label="Campaign summary">
          <div>
            <p className="campaign-id">{scenario.id}</p>
            <h1>{scenario.title}</h1>
          </div>
          <div className="campaign-best">
            <span>Best {campaign.metric?.name.toLowerCase()}</span>
            <strong>{formatMetric(best, campaign.metric?.unit)}</strong>
          </div>
        </section>
        <Sheet
          open={campaign.view === 'objective'}
          onOpenChange={open => campaign.setView(open ? 'objective' : 'dashboard')}
        >
          <section className="objective-brief" aria-labelledby="objective-brief-title">
            <div>
              <span className="objective-label">Objective</span>
              <h2 id="objective-brief-title">{scenario.objective.title}</h2>
              <p>{scenario.objective.statement}</p>
            </div>
            <SheetTrigger asChild>
              <Button
                type="button"
                variant="outline"
                size="sm"
                className="objective-details-trigger"
                aria-label="Objective details"
              >
                Objective details
              </Button>
            </SheetTrigger>
          </section>
          <SheetContent className="objective-sheet">
            <div className="dialog-head">
              <div>
                <p className="section-kicker">CAMPAIGN OBJECTIVE</p>
                <SheetTitle>{scenario.objective.title}</SheetTitle>
              </div>
              <SheetClose asChild>
                <button type="button" className="dialog-close" aria-label="Close objective details">
                  ×
                </button>
              </SheetClose>
            </div>
            <p className="dialog-hypothesis">{scenario.objective.statement}</p>
            <ObjectiveDetails scenario={scenario} />
          </SheetContent>
        </Sheet>
      </div>
    </>
  );
}

function CampaignOverview({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const snapshot = workstreamSnapshot(scenario, campaign.latestTimestamp);
  return (
    <>
      <div className="campaign-workspace">
        <ActiveWorkPanel snapshot={snapshot} campaign={campaign} />
        <PerformancePanel scenario={scenario} campaign={campaign} />
        <MeasurementSummary scenario={scenario} campaign={campaign} />
      </div>
      <CampaignTimeline scenario={scenario} campaign={campaign} />
      <WorkstreamsSection snapshot={snapshot} campaign={campaign} />
    </>
  );
}

function PerformancePanel({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const visibleMeasurements = measurementsThrough(
    campaign.orderedMeasurements,
    campaign.pointIndex,
  );
  const benchmarkChanged = new Set(visibleMeasurements.map(item => item.benchmarkVersion)).size > 1;
  return (
    <section className="panel performance-panel" aria-labelledby="performance-title">
      <div className="section-head performance-head">
        <div>
          <p className="section-kicker">
            MEASUREMENTS <span>·</span> {campaign.orderedMeasurements.length} recorded
          </p>
          <h2 id="performance-title">Performance trajectory</h2>
        </div>
        <div className="performance-meta">
          <span className="metric-readout">
            {campaign.metric?.name} <i>·</i> {campaign.metric?.unit}
          </span>
          <span className="chart-key" aria-hidden="true">
            <span>
              <i className="key-run" /> Runs
            </span>
            <span>
              <i className="key-best" /> Running best
            </span>
          </span>
          {benchmarkChanged && <span className="benchmark-warning">Benchmark changed mid-run</span>}
        </div>
      </div>
      <PerformanceChart
        scenario={scenario}
        measurements={campaign.orderedMeasurements}
        throughIndex={campaign.pointIndex}
        metricId={campaign.metric?.id ?? ''}
        selectedIndex={campaign.pointIndex}
        onSelect={campaign.setPointIndex}
      />
      <CampaignControls campaign={campaign} />
    </section>
  );
}

function CampaignControls({campaign}: {readonly campaign: CampaignViewModel}): JSX.Element {
  const move = (offset: number): void => {
    campaign.setPlaying(false);
    campaign.setPointIndex(campaign.pointIndex + offset);
  };
  return (
    <div className="campaign-controls">
      <div className="playback-buttons">
        <button type="button" aria-label="Previous measurement" onClick={() => move(-1)}>
          ‹
        </button>
        <button
          type="button"
          className="play-button"
          aria-label={campaign.playing ? 'Pause campaign' : 'Play campaign'}
          onClick={() => campaign.setPlaying(!campaign.playing)}
        >
          {campaign.playing ? 'Ⅱ' : '▶'}
        </button>
        <button type="button" aria-label="Next measurement" onClick={() => move(1)}>
          ›
        </button>
      </div>
      <label className="scrubber">
        <span className="sr-only">Campaign measurement</span>
        <input
          type="range"
          min="0"
          max={Math.max(0, campaign.orderedMeasurements.length - 1)}
          value={campaign.pointIndex}
          onChange={event => {
            campaign.setPlaying(false);
            campaign.setPointIndex(Number(event.target.value));
          }}
        />
      </label>
      <span className="scrubber-count">
        {String(campaign.pointIndex + 1).padStart(2, '0')} <i>/</i>{' '}
        {String(campaign.orderedMeasurements.length).padStart(2, '0')}
      </span>
    </div>
  );
}

function MeasurementSummary({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const measurement = campaign.activeMeasurement;
  return (
    <section className="selected-measurement" aria-label="Selected measurement" aria-live="polite">
      <h2>Selected measurement</h2>
      <div className="selected-measurement-main">
        <span
          className={`disposition-dot disposition-${measurement?.disposition ?? 'inconclusive'}`}
        />
        <div>
          <strong>
            {measurement?.label ?? workstreamName(scenario, measurement?.workstreamId)}
          </strong>
          <span>
            {measurement?.benchmarkVersion}
            {measurement?.sourceOrder === null || measurement?.sourceOrder === undefined
              ? ''
              : ` · source order ${measurement.sourceOrder}`}{' '}
            <i>·</i> {formatTimestamp(measurement?.timestamp)}
          </span>
        </div>
      </div>
      <div className="selected-measurement-detail">
        {measurement?.triggeredByAgentId != null && (
          <button
            type="button"
            onClick={() => campaign.setSelectedAgentId(measurement.triggeredByAgentId)}
          >
            Triggered by {agentName(scenario, measurement.triggeredByAgentId)}
          </button>
        )}
        {measurement?.runnerAgentId != null && (
          <button
            type="button"
            onClick={() => campaign.setSelectedAgentId(measurement.runnerAgentId)}
          >
            Run by {agentName(scenario, measurement.runnerAgentId)}
          </button>
        )}
      </div>
      <div className="selected-measurement-gates">
        {measurement?.gates.map(gate => (
          <span className={`gate-pill gate-${gate.status}`} key={gate.gateId} title={gate.detail}>
            {gate.status} · {gate.gateId}
          </span>
        ))}
      </div>
      <div className="selected-values">
        {measurement?.values.map(value => (
          <span key={value.metricId}>
            <b>{metricName(scenario, value.metricId)}</b> {formatMetric(value.value, value.unit)}
          </span>
        ))}
      </div>
      <button
        type="button"
        className="text-button"
        onClick={() => measurement && campaign.setSelectedWorkstreamId(measurement.workstreamId)}
      >
        Inspect workstream <span aria-hidden="true">↗</span>
      </button>
    </section>
  );
}

interface WorkstreamSnapshot {
  readonly asOfTime: number;
  readonly active: Workstream[];
  readonly completed: Workstream[];
  readonly acceptedCount: number;
}

function workstreamSnapshot(scenario: CampaignRecord, latestTimestamp: string): WorkstreamSnapshot {
  const asOfTime = Date.parse(latestTimestamp);
  const visible = scenario.workstreams.filter(item => Date.parse(item.startedAt) <= asOfTime);
  const active = visible
    .filter(item => workstreamState(item, asOfTime) === 'active')
    .sort((left, right) => Date.parse(right.startedAt) - Date.parse(left.startedAt));
  const completed = visible.filter(item => workstreamState(item, asOfTime) !== 'active');
  const acceptedCount = completed.filter(item => item.outcome === 'accepted').length;
  return {asOfTime, active, completed, acceptedCount};
}

function ActiveWorkPanel({
  snapshot,
  campaign,
}: {
  readonly snapshot: WorkstreamSnapshot;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const {active, completed, acceptedCount, asOfTime} = snapshot;
  return (
    <section className="active-workstreams" aria-label="Active workstreams">
      <div className="active-work-header">
        <h2>Current work</h2>
        <strong>{active.length} active</strong>
      </div>
      <p className="workstreams-counts">
        {acceptedCount} accepted <span>·</span> {completed.length - acceptedCount} rejected
      </p>
      <div className="active-workstream-list">
        {active.length === 0 ? (
          <p className="active-work-empty">No active workstreams.</p>
        ) : (
          active.map(item => (
            <button
              type="button"
              key={item.id}
              aria-current={
                item.id === campaign.activeMeasurement?.workstreamId ? 'true' : undefined
              }
              aria-label={`${item.title}, ${formatDuration(workstreamDuration(item, asOfTime))} elapsed${item.id === campaign.activeMeasurement?.workstreamId ? ', contains selected measurement' : ''}. Open workstream.`}
              onClick={() => campaign.setSelectedWorkstreamId(item.id)}
            >
              <strong>{item.title}</strong>
              <span>{formatDuration(workstreamDuration(item, asOfTime))} elapsed</span>
            </button>
          ))
        )}
      </div>
    </section>
  );
}

function WorkstreamsSection({
  snapshot,
  campaign,
}: {
  readonly snapshot: WorkstreamSnapshot;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const [query, setQuery] = useState('');
  const [statusFilter, setStatusFilter] = useState<'all' | 'accepted' | 'rejected'>('all');
  const {asOfTime, completed} = snapshot;
  const search = query.trim().toLowerCase();
  const history = sortWorkstreams(
    completed.filter(
      item =>
        (statusFilter === 'all' || item.outcome === statusFilter) &&
        `${item.title} ${item.hypothesis} ${item.outcomeSummary}`.toLowerCase().includes(search),
    ),
    campaign.workstreamSort,
    campaign.workstreamTokenSpend,
    asOfTime,
  );
  return (
    <section className="section-block workstreams-section" aria-labelledby="workstreams-title">
      <div className="section-head history-heading">
        <div>
          <h2 id="workstreams-title">Work history</h2>
          <span aria-live="polite">
            {history.length} of {completed.length} workstreams
          </span>
        </div>
      </div>
      <section className="history-section" aria-label="Completed workstreams">
        <div className="history-toolbar">
          <label htmlFor="completed-workstream-search">
            <span className="sr-only">Search completed workstreams</span>
            <Input
              id="completed-workstream-search"
              type="search"
              name="completed-workstream-search"
              autoComplete="off"
              spellCheck={false}
              aria-label="Search completed workstreams"
              placeholder="Search workstreams"
              value={query}
              onChange={event => setQuery(event.target.value)}
            />
          </label>
          <label htmlFor="completed-workstream-outcome">
            <span className="sr-only">Filter history by outcome</span>
            <NativeSelect
              id="completed-workstream-outcome"
              size="sm"
              aria-label="Filter history by outcome"
              value={statusFilter}
              onChange={event =>
                setStatusFilter(event.target.value as 'all' | 'accepted' | 'rejected')
              }
            >
              <NativeSelectOption value="all">All outcomes</NativeSelectOption>
              <NativeSelectOption value="accepted">Accepted</NativeSelectOption>
              <NativeSelectOption value="rejected">Rejected</NativeSelectOption>
            </NativeSelect>
          </label>
        </div>
        <WorkstreamHistory
          workstreams={history}
          asOfTime={asOfTime}
          tokenSpend={campaign.workstreamTokenSpend}
          selectedMeasurementWorkstreamId={campaign.activeMeasurement?.workstreamId}
          controlsKey={`${query}|${statusFilter}|${campaign.workstreamSort}`}
          sort={campaign.workstreamSort}
          onSort={campaign.setWorkstreamSort}
          onSelect={campaign.setSelectedWorkstreamId}
        />
      </section>
    </section>
  );
}

function ObjectiveDetails({scenario}: {readonly scenario: CampaignRecord}): JSX.Element {
  return (
    <div className="objective-details">
      <section>
        <h3>Metrics</h3>
        <div className="metric-definition-list">
          {scenario.objective.metrics.map(item => (
            <article key={item.id}>
              <div>
                <strong>{item.name}</strong>
                <span>
                  {item.direction} · {item.unit}
                </span>
              </div>
              <p>{item.description}</p>
            </article>
          ))}
        </div>
      </section>
      {scenario.objective.gates.length > 0 && (
        <section>
          <h3>Gates</h3>
          <ul>
            {scenario.objective.gates.map(gate => (
              <li key={gate.id}>
                <strong>{gate.label}</strong> {gate.description}
              </li>
            ))}
          </ul>
        </section>
      )}
      {scenario.objective.constraints.length > 0 && (
        <section>
          <h3>Constraints</h3>
          <ul>
            {scenario.objective.constraints.map(constraint => (
              <li key={constraint}>{constraint}</li>
            ))}
          </ul>
        </section>
      )}
      <details className="objective-data-notes">
        <summary>Data notes</summary>
        <p>{scenario.provenance}</p>
      </details>
    </div>
  );
}

interface PerformanceChartProps {
  readonly scenario: CampaignRecord;
  readonly measurements: CampaignRecord['measurements'];
  readonly throughIndex: number;
  readonly metricId: string;
  readonly selectedIndex: number;
  readonly onSelect: (index: number) => void;
}

function measurementsThrough(
  measurements: CampaignRecord['measurements'],
  throughIndex: number,
): CampaignRecord['measurements'] {
  const length = Math.max(0, Math.min(measurements.length, throughIndex + 1));
  return measurements.slice(0, length);
}

function chartModel({
  scenario,
  measurements,
  throughIndex,
  metricId,
  selectedIndex,
}: PerformanceChartProps) {
  const metric = scenario.objective.metrics.find(item => item.id === metricId);
  const visibleMeasurements = measurementsThrough(measurements, throughIndex);
  const allPoints = measurements
    .map((measurement, index) => ({measurement, index}))
    .filter(item => item.measurement.values.some(value => value.metricId === metricId));
  const points = visibleMeasurements
    .map((measurement, index) => ({measurement, index}))
    .filter(item => item.measurement.values.some(value => value.metricId === metricId));
  const values = points
    .map(({measurement}) => measurement.values.find(value => value.metricId === metricId)?.value)
    .filter((value): value is number => value !== undefined);
  const rawMin = values.length === 0 ? 0 : Math.min(...values);
  const rawMax = values.length === 0 ? 1 : Math.max(...values);
  const padding = Math.max((rawMax - rawMin) * 0.08, Math.abs(rawMax) * 0.025, 1);
  const domain = {min: rawMin >= 0 ? 0 : rawMin - padding, max: rawMax + padding};
  const y = (value: number): number => {
    return (
      CHART.bottom - ((value - domain.min) / (domain.max - domain.min)) * (CHART.bottom - CHART.top)
    );
  };
  const x = (index: number): number =>
    CHART.left +
    (measurements.length < 2 ? 0 : index / (measurements.length - 1)) * (CHART.right - CHART.left);
  const selected = measurements[selectedIndex];
  const selectedValue = selected?.values.find(value => value.metricId === metricId)?.value;
  return {domain, metric, allPoints, points, selected, selectedValue, x, y};
}

type ChartModel = ReturnType<typeof chartModel>;

function PerformanceChart(props: PerformanceChartProps): JSX.Element {
  const model = chartModel(props);
  return (
    <div className="chart-wrap">
      <ChartAxis model={model} />
      <ChartPlot {...props} model={model} />
    </div>
  );
}

function ChartAxis({model}: {readonly model: ChartModel}): JSX.Element {
  return (
    <div className="chart-y-axis chart-y-axis-left" aria-hidden="true">
      {axisTicks(model.domain).map(tick => (
        <span key={tick}>{formatCompact(tick, model.metric?.unit)}</span>
      ))}
    </div>
  );
}

function ChartPlot(props: PerformanceChartProps & {readonly model: ChartModel}): JSX.Element {
  const {metric, selected, selectedValue, x, y} = props.model;
  const chartRef = useRef<SVGSVGElement>(null);
  const pendingFocus = useRef<number | null>(null);
  const followChart = (chart: SVGSVGElement | null): void => {
    chartRef.current = chart;
    const index = pendingFocus.current;
    if (index === null) return;
    const button = chartRef.current?.querySelector<HTMLButtonElement>(
      `button[data-chart-index="${index}"]`,
    );
    if (button !== undefined && button !== null) {
      button.focus();
      pendingFocus.current = null;
    }
  };

  const onPointKeyDown = (index: number, event: KeyboardEvent<HTMLButtonElement>): void => {
    const key = event.key;
    if (key !== 'ArrowLeft' && key !== 'ArrowRight' && key !== 'Home' && key !== 'End') return;
    event.preventDefault();
    const allPoints = props.model.allPoints;
    const current = allPoints.findIndex(item => item.index === index);
    const next =
      key === 'Home'
        ? 0
        : key === 'End'
          ? allPoints.length - 1
          : Math.max(0, Math.min(allPoints.length - 1, current + (key === 'ArrowRight' ? 1 : -1)));
    const target = allPoints[next];
    if (target === undefined) return;
    pendingFocus.current = target.index;
    props.onSelect(target.index);
  };
  const rovingIndex = props.model.points.some(item => item.index === props.selectedIndex)
    ? props.selectedIndex
    : props.model.points.at(-1)?.index;
  return (
    <fieldset
      className="chart-group"
      aria-label={`${metric?.name ?? 'Performance'} measurements by campaign order. Gray dots are individual runs; the step line is the running best. Tab to a point, then use arrow keys, Home, or End to select measurements.`}
    >
      <svg
        ref={followChart}
        className="performance-chart"
        viewBox="0 0 960 300"
        role="presentation"
      >
        {[0, 0.5, 1].map(ratio => (
          <line
            key={ratio}
            className="chart-gridline"
            x1={CHART.left}
            x2={CHART.right}
            y1={CHART.top + ratio * (CHART.bottom - CHART.top)}
            y2={CHART.top + ratio * (CHART.bottom - CHART.top)}
          />
        ))}
        <ChartSeries props={props} rovingIndex={rovingIndex} onPointKeyDown={onPointKeyDown} />
        {selectedValue !== undefined && selected !== undefined && (
          <line
            className="selected-guide"
            x1={x(props.selectedIndex)}
            x2={x(props.selectedIndex)}
            y1={y(selectedValue)}
            y2={CHART.bottom}
          />
        )}
        <text className="chart-axis-label" x={CHART.left} y="288">
          Earlier experiments
        </text>
        <text className="chart-axis-label" x={CHART.right} y="288" textAnchor="end">
          Later experiments
        </text>
      </svg>
    </fieldset>
  );
}

function ChartSeries({
  props,
  rovingIndex,
  onPointKeyDown,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
  readonly rovingIndex: number | undefined;
  readonly onPointKeyDown: (index: number, event: KeyboardEvent<HTMLButtonElement>) => void;
}): JSX.Element {
  const path = runningBestPath(props);
  return (
    <g>
      <path className="performance-best-line" d={path} />
      {props.model.points.map(({measurement, index}) => (
        <ChartPoint
          key={measurement.id}
          props={props}
          measurement={measurement}
          index={index}
          rovingIndex={rovingIndex}
          onPointKeyDown={onPointKeyDown}
        />
      ))}
    </g>
  );
}

function runningBestPath(props: PerformanceChartProps & {readonly model: ChartModel}): string {
  let best: number | undefined;
  let path = '';
  for (const {measurement, index} of props.model.points) {
    const value = measurement.values.find(item => item.metricId === props.metricId)?.value;
    if (value === undefined) continue;
    const pointX = props.model.x(index);
    if (best === undefined) {
      best = value;
      path = `M ${pointX} ${props.model.y(best)}`;
      continue;
    }
    path += ` H ${pointX}`;
    const better = props.model.metric?.direction === 'minimize' ? value < best : value > best;
    if (better) {
      best = value;
      path += ` V ${props.model.y(best)}`;
    }
  }
  if (best !== undefined) path += ` H ${props.model.x(props.throughIndex)}`;
  return path;
}

function ChartPoint({
  props,
  measurement,
  index,
  rovingIndex,
  onPointKeyDown,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
  readonly measurement: CampaignRecord['measurements'][number];
  readonly index: number;
  readonly rovingIndex: number | undefined;
  readonly onPointKeyDown: (index: number, event: KeyboardEvent<HTMLButtonElement>) => void;
}): JSX.Element | null {
  const value = measurement.values.find(item => item.metricId === props.metricId)?.value;
  if (value === undefined) return null;
  const cx = props.model.x(index),
    cy = props.model.y(value);
  const label = `${measurement.label}, ${formatMetric(value, props.model.metric?.unit)}, ${measurement.disposition}. Select measurement.`;
  return (
    <g className="chart-point-group">
      <circle
        className={`chart-point${index === props.selectedIndex ? ' point-selected' : ''}`}
        cx={cx}
        cy={cy}
        r={index === props.selectedIndex ? 6 : 3}
      />
      <foreignObject x={cx - 9} y={cy - 9} width="18" height="18">
        <button
          type="button"
          className="chart-point-button"
          data-chart-index={index}
          tabIndex={index === rovingIndex ? 0 : -1}
          aria-current={index === props.selectedIndex ? 'true' : undefined}
          aria-label={label}
          onKeyDown={event => onPointKeyDown(index, event)}
          onClick={() => props.onSelect(index)}
        />
      </foreignObject>
    </g>
  );
}

type Workstream = CampaignRecord['workstreams'][number];
type WorkstreamState = 'active' | 'accepted' | 'rejected';

const HISTORY_ROW_HEIGHT = 52;
const HISTORY_VIEWPORT_HEIGHT = 312;
const HISTORY_OVERSCAN = 4;

function historyWindow(scrollTop: number, count: number): {first: number; last: number} {
  return {
    first: Math.max(0, Math.floor(scrollTop / HISTORY_ROW_HEIGHT) - HISTORY_OVERSCAN),
    last: Math.min(
      count,
      Math.ceil((scrollTop + HISTORY_VIEWPORT_HEIGHT) / HISTORY_ROW_HEIGHT) + HISTORY_OVERSCAN,
    ),
  };
}

interface WorkstreamHistoryProps {
  readonly workstreams: Workstream[];
  readonly asOfTime: number;
  readonly tokenSpend: Readonly<Record<string, number>>;
  readonly selectedMeasurementWorkstreamId: string | undefined;
  readonly controlsKey: string;
  readonly sort: CampaignViewModel['workstreamSort'];
  readonly onSort: CampaignViewModel['setWorkstreamSort'];
  readonly onSelect: (id: string) => void;
}

function WorkstreamHistory({
  workstreams,
  asOfTime,
  tokenSpend,
  selectedMeasurementWorkstreamId,
  controlsKey,
  sort,
  onSort,
  onSelect,
}: WorkstreamHistoryProps): JSX.Element {
  const viewportRef = useRef<HTMLTableSectionElement>(null);
  const hasUsage = Object.keys(tokenSpend).length > 0;
  const previous = useRef<{ids: string[]; controlsKey: string} | null>(null);
  const [scrollTop, setScrollTop] = useState(0);
  const [focusedId, setFocusedId] = useState<string | null>(null);
  const committedFocus = useRef<string | null>(null);

  const followViewport = (viewport: HTMLTableSectionElement | null): void => {
    viewportRef.current = viewport;
    if (viewport === null) return;
    const before = previous.current;
    // Read the scroll position directly: a live update can commit before the scroll event fires.
    const oldTop = viewport.scrollTop;
    if (before?.controlsKey !== controlsKey) {
      viewport.scrollTop = 0;
    } else if (before !== null) {
      const anchorId = before.ids[Math.floor(oldTop / HISTORY_ROW_HEIGHT)];
      const anchorIndex = workstreams.findIndex(item => item.id === anchorId);
      viewport.scrollTop =
        anchorIndex >= 0
          ? anchorIndex * HISTORY_ROW_HEIGHT + (oldTop % HISTORY_ROW_HEIGHT)
          : Math.min(
              oldTop,
              Math.max(0, workstreams.length * HISTORY_ROW_HEIGHT - HISTORY_VIEWPORT_HEIGHT),
            );
    }
    setScrollTop(viewport.scrollTop);
    previous.current = {
      ids: workstreams.map(item => item.id),
      controlsKey,
    };
    if (committedFocus.current !== focusedId) {
      committedFocus.current = focusedId;
      focusHistoryRow(viewport, focusedId);
    }
  };

  const {first, last} = historyWindow(scrollTop, workstreams.length);

  const onArrow = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return;
    const viewport = viewportRef.current;
    if (viewport === null || workstreams.length === 0) return;
    event.preventDefault();
    const current = workstreams.findIndex(item => item.id === focusedId);
    const next = Math.max(
      0,
      Math.min(workstreams.length - 1, current + (event.key === 'ArrowDown' ? 1 : -1)),
    );
    const item = workstreams[next];
    if (item === undefined) return;
    revealHistoryRow(viewport, next);
    setScrollTop(viewport.scrollTop);
    setFocusedId(item.id);
  };

  return (
    <table
      className={hasUsage ? 'history-results' : 'history-results history-no-usage'}
      aria-label="Work history"
      aria-rowcount={workstreams.length + 1}
    >
      <thead>
        <HistoryColumns hasUsage={hasUsage} sort={sort} onSort={onSort} />
      </thead>
      <tbody
        className="history-viewport"
        ref={followViewport}
        aria-label="Completed history results"
        style={
          {'--history-height': `${workstreams.length * HISTORY_ROW_HEIGHT}px`} as CSSProperties
        }
        onKeyDown={onArrow}
        onScroll={event => setScrollTop(event.currentTarget.scrollTop)}
      >
        {workstreams.length === 0 ? (
          <tr>
            <td colSpan={hasUsage ? 6 : 5}>
              <p className="history-empty">No completed workstreams match this view.</p>
            </td>
          </tr>
        ) : (
          workstreams
            .slice(first, last)
            .map((item, offset) => (
              <HistoryRow
                key={item.id}
                item={item}
                index={first + offset}
                asOfTime={asOfTime}
                hasUsage={hasUsage}
                tokenSpend={tokenSpend[item.id]}
                selectedMeasurementWorkstreamId={selectedMeasurementWorkstreamId}
                onFocus={setFocusedId}
                onSelect={onSelect}
              />
            ))
        )}
      </tbody>
    </table>
  );
}

const HISTORY_COLUMNS = [
  {key: 'title', label: 'Workstream'},
  {key: 'outcome', label: 'Outcome'},
  {key: 'duration', label: 'Elapsed'},
  {key: 'start', label: 'Started'},
  {key: 'end', label: 'Ended'},
  {key: 'tokens', label: 'Tokens'},
] as const;

function HistoryColumns({
  hasUsage,
  sort,
  onSort,
}: {
  readonly hasUsage: boolean;
  readonly sort: CampaignViewModel['workstreamSort'];
  readonly onSort: CampaignViewModel['setWorkstreamSort'];
}): JSX.Element {
  return (
    <tr className="history-columns">
      {HISTORY_COLUMNS.filter(column => hasUsage || column.key !== 'tokens').map(column => {
        const selected = sort.startsWith(`${column.key}-`);
        const ascending = sort.endsWith('-asc');
        return (
          <th
            key={column.key}
            scope="col"
            aria-sort={selected ? (ascending ? 'ascending' : 'descending') : 'none'}
          >
            <button
              type="button"
              aria-label={`Sort by ${column.label}`}
              onClick={() => onSort(`${column.key}-${selected && ascending ? 'desc' : 'asc'}`)}
            >
              {column.label}
              <span aria-hidden="true">{selected ? (ascending ? '↑' : '↓') : '↕'}</span>
            </button>
          </th>
        );
      })}
    </tr>
  );
}

function revealHistoryRow(viewport: HTMLElement, index: number): void {
  const top = index * HISTORY_ROW_HEIGHT;
  if (top < viewport.scrollTop) viewport.scrollTop = top;
  else if (top + HISTORY_ROW_HEIGHT > viewport.scrollTop + HISTORY_VIEWPORT_HEIGHT)
    viewport.scrollTop = top + HISTORY_ROW_HEIGHT - HISTORY_VIEWPORT_HEIGHT;
}

function focusHistoryRow(viewport: HTMLElement | null, id: string | null): void {
  if (viewport === null || id === null) return;
  const buttons = viewport.querySelectorAll<HTMLButtonElement>('[data-workstream-id]');
  for (const button of buttons) {
    if (button.dataset['workstreamId'] === id) {
      button.focus();
      break;
    }
  }
}

function HistoryRow({
  item,
  index,
  asOfTime,
  hasUsage,
  tokenSpend,
  selectedMeasurementWorkstreamId,
  onFocus,
  onSelect,
}: {
  readonly item: Workstream;
  readonly index: number;
  readonly asOfTime: number;
  readonly hasUsage: boolean;
  readonly tokenSpend: number | undefined;
  readonly selectedMeasurementWorkstreamId: string | undefined;
  readonly onFocus: (id: string) => void;
  readonly onSelect: (id: string) => void;
}): JSX.Element {
  const elapsed = formatDuration(workstreamDuration(item, asOfTime));
  return (
    <tr className="history-row" aria-rowindex={index + 2} style={{top: index * HISTORY_ROW_HEIGHT}}>
      <td>
        <button
          type="button"
          data-workstream-id={item.id}
          aria-current={item.id === selectedMeasurementWorkstreamId ? 'true' : undefined}
          aria-label={`${item.title}, ${item.outcome}, ${elapsed} elapsed, ended ${formatTimestamp(item.finishedAt)}${hasUsage ? `, ${formatTokenSpend(tokenSpend)}` : ''}${item.id === selectedMeasurementWorkstreamId ? ', contains selected measurement' : ''}. Open workstream.`}
          onFocus={() => onFocus(item.id)}
          onClick={() => onSelect(item.id)}
        >
          <strong title={item.title}>{item.title}</strong>
        </button>
      </td>
      <td>
        <span className={`outcome-chip chip-${item.outcome}`}>{item.outcome}</span>
      </td>
      <td>{elapsed}</td>
      <td title={formatTimestamp(item.startedAt)}>{formatDate(Date.parse(item.startedAt))}</td>
      <td title={formatTimestamp(item.finishedAt)}>{formatDate(Date.parse(item.finishedAt))}</td>
      {hasUsage && <td>{formatTokenSpend(tokenSpend)}</td>}
    </tr>
  );
}

function workstreamState(workstream: Workstream, asOfTime: number): WorkstreamState {
  if (Date.parse(workstream.finishedAt) > asOfTime) return 'active';
  return workstream.outcome;
}

function workstreamDuration(workstream: Workstream, asOfTime: number): number {
  return Math.max(
    0,
    Math.min(Date.parse(workstream.finishedAt), asOfTime) - Date.parse(workstream.startedAt),
  );
}

function sortWorkstreams(
  workstreams: Workstream[],
  sort: CampaignViewModel['workstreamSort'],
  tokenSpend: Readonly<Record<string, number>>,
  asOfTime: number,
): Workstream[] {
  const direction = sort.endsWith('-asc') ? 1 : -1;
  return [...workstreams].sort((left, right) => {
    if (sort.startsWith('title')) return direction * left.title.localeCompare(right.title);
    if (sort.startsWith('outcome')) return direction * left.outcome.localeCompare(right.outcome);
    if (sort.startsWith('start'))
      return direction * (Date.parse(left.startedAt) - Date.parse(right.startedAt));
    if (sort.startsWith('end')) {
      const endComparison =
        direction *
        (Math.min(Date.parse(left.finishedAt), asOfTime) -
          Math.min(Date.parse(right.finishedAt), asOfTime));
      return endComparison || Date.parse(right.startedAt) - Date.parse(left.startedAt);
    }
    if (sort.startsWith('duration'))
      return direction * (workstreamDuration(left, asOfTime) - workstreamDuration(right, asOfTime));
    return compareNullable(tokenSpend[left.id], tokenSpend[right.id], direction);
  });
}

function compareNullable(left: number | undefined, right: number | undefined, direction: number) {
  if (left === undefined) return right === undefined ? 0 : 1;
  if (right === undefined) return -1;
  return direction * (left - right);
}

function WorkstreamDialog({
  scenario,
  workstream,
  cursorSequence,
  cursorTimestamp,
  returnFocus,
  onClose,
  onAgent,
}: {
  readonly scenario: CampaignRecord;
  readonly workstream: CampaignRecord['workstreams'][number];
  readonly cursorSequence: number;
  readonly cursorTimestamp: string;
  readonly returnFocus: HTMLElement | null;
  readonly onClose: () => void;
  readonly onAgent: (id: string, opener: HTMLElement | null) => void;
}): JSX.Element {
  const opener = useRef<HTMLElement | null>(null);
  const finished = Date.parse(workstream.finishedAt) <= Date.parse(cursorTimestamp);
  const disposition = finished ? workstream.outcome : 'active';
  const measurements = scenario.measurements.filter(
    item => item.workstreamId === workstream.id && item.sequence <= cursorSequence,
  );
  const agents = scenario.agents.filter(agent => agent.workstreamIds.includes(workstream.id));
  const turnCounts = new Map(
    agents.map(agent => [
      agent.id,
      scenario.trajectories
        .find(item => item.agentId === agent.id)
        ?.turns.filter(
          turn =>
            turn.workstreamId === workstream.id &&
            Date.parse(turn.startedAt) <= Date.parse(cursorTimestamp),
        ).length ?? 0,
    ]),
  );
  const totalTurns = [...turnCounts.values()].reduce((total, count) => total + count, 0);
  const close = (): void => {
    onClose();
    queueMicrotask(() => restoreInspectorFocus(opener.current));
  };
  return (
    <Sheet open onOpenChange={open => !open && close()}>
      <SheetContent
        className="detail-dialog"
        onOpenAutoFocus={() => {
          opener.current =
            returnFocus?.isConnected === true
              ? returnFocus
              : document.activeElement instanceof HTMLElement
                ? document.activeElement
                : null;
        }}
        onCloseAutoFocus={event => event.preventDefault()}
      >
        <div className="dialog-head">
          <div>
            <p className="section-kicker">
              WORKSTREAM DETAIL <span>·</span> {workstream.id}
            </p>
            <SheetTitle>{workstream.title}</SheetTitle>
          </div>
          <SheetClose asChild>
            <button type="button" className="dialog-close" aria-label="Close workstream detail">
              ×
            </button>
          </SheetClose>
        </div>
        <span className={`outcome-chip chip-${disposition}`}>
          {finished ? workstream.outcome : 'in progress'}
        </span>
        <p className="dialog-hypothesis">{workstream.hypothesis}</p>
        <div className="detail-result">
          <span>{finished ? 'Disposition' : 'Current status'}</span>
          <strong>
            {finished
              ? workstream.outcomeSummary
              : 'This workstream is in progress at the selected campaign position.'}
          </strong>
        </div>
        <div className="detail-meta">
          <div>
            <span>Window</span>
            <strong>
              {formatTimestamp(workstream.startedAt)} – {formatTimestamp(workstream.finishedAt)}
            </strong>
          </div>
          <div>
            <span>Measurements</span>
            <strong>{measurements.length}</strong>
          </div>
        </div>
        <h3>
          Agent turn share <small>based on curated turn counts</small>
        </h3>
        {agents.length === 0 ? (
          <p className="empty-state">No agent attribution was recorded.</p>
        ) : (
          <div className="time-split">
            {agents.map((agent, index) => {
              const count = turnCounts.get(agent.id) ?? 0;
              const share = totalTurns === 0 ? 0 : (count / totalTurns) * 100;
              return (
                <button
                  type="button"
                  className="split-row"
                  key={agent.id}
                  onClick={() => onAgent(agent.id, opener.current)}
                >
                  <span className="split-label">
                    <span
                      className="split-swatch"
                      style={
                        {'--agent-color': ROLE_COLORS[index % ROLE_COLORS.length]} as CSSProperties
                      }
                    />
                    <span>
                      <strong>{agent.name}</strong>
                      <small>
                        {agent.role} · {count} {count === 1 ? 'turn' : 'turns'}
                      </small>
                    </span>
                    <b>{Math.round(share)}%</b>
                  </span>
                  <span className="split-track">
                    <i
                      style={
                        {
                          width: `${share}%`,
                          '--agent-color': ROLE_COLORS[index % ROLE_COLORS.length],
                        } as CSSProperties
                      }
                    />
                  </span>
                </button>
              );
            })}
          </div>
        )}
        <h3>
          Measurements <small>{measurements.length} observations</small>
        </h3>
        <div className="detail-measurements">
          {measurements.map(item => (
            <article key={item.id}>
              <span className={`disposition-dot disposition-${item.disposition}`} />
              <div>
                <strong>
                  {item.benchmarkVersion} · {formatTimestamp(item.timestamp)}
                </strong>
                <div className="gate-pills">
                  {item.gates.map(gate => (
                    <span className={`gate-pill gate-${gate.status}`} key={gate.gateId}>
                      {gate.status} · {gate.gateId}
                    </span>
                  ))}
                </div>
              </div>
              <span>
                {item.values.map(value => formatMetric(value.value, value.unit)).join(' · ')}
              </span>
            </article>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
}

function AgentDialog({
  scenario,
  agentId,
  cursorSequence,
  cursorTimestamp,
  onClose,
  onBack,
  onWorkstream,
}: {
  readonly scenario: CampaignRecord;
  readonly agentId: string;
  readonly cursorSequence: number;
  readonly cursorTimestamp: string;
  readonly onClose: () => void;
  readonly onBack: (() => void) | undefined;
  readonly onWorkstream: (id: string) => void;
}): JSX.Element | null {
  const opener = useRef<HTMLElement | null>(null);
  const agent = scenario.agents.find(item => item.id === agentId);
  const turns = (scenario.trajectories.find(item => item.agentId === agentId)?.turns ?? []).filter(
    turn =>
      Date.parse(turn.startedAt) <= Date.parse(cursorTimestamp) &&
      (scenario.workstreams.find(workstream => workstream.id === turn.workstreamId)
        ?.firstSequence ?? Number.POSITIVE_INFINITY) <= cursorSequence,
  );
  if (agent === undefined) return null;
  const close = (): void => {
    onClose();
    queueMicrotask(() => restoreInspectorFocus(opener.current));
  };
  return (
    <Sheet open onOpenChange={open => !open && close()}>
      <SheetContent
        className="detail-dialog trajectory-dialog"
        onOpenAutoFocus={() => {
          opener.current =
            document.activeElement instanceof HTMLElement ? document.activeElement : null;
        }}
        onCloseAutoFocus={event => event.preventDefault()}
      >
        <div className="dialog-head">
          <div>
            <p className="section-kicker">
              AGENT TRAJECTORY <span>·</span> {agent.role}
            </p>
            <SheetTitle>{agent.name}</SheetTitle>
          </div>
          <div className="dialog-actions">
            {onBack !== undefined && (
              <button type="button" className="text-button" onClick={onBack}>
                Back to workstream
              </button>
            )}
            <SheetClose asChild>
              <button type="button" className="dialog-close" aria-label="Close agent trajectory">
                ×
              </button>
            </SheetClose>
          </div>
        </div>
        <p className="dialog-hypothesis">
          {agent.role} · {turns.length} available turns across{' '}
          {new Set(turns.map(turn => turn.workstreamId)).size} workstreams
        </p>
        {turns.length === 0 ? (
          <p className="empty-state">No turn-level trajectory is available for this agent.</p>
        ) : (
          <ol className="turn-list">
            {turns.map(turn => (
              <li key={turn.id} className="turn-card">
                <div className="turn-heading">
                  <span className="turn-ordinal">TURN {String(turn.ordinal).padStart(2, '0')}</span>
                  <button
                    type="button"
                    className="text-button"
                    onClick={() => onWorkstream(turn.workstreamId)}
                  >
                    {workstreamName(scenario, turn.workstreamId)} <span aria-hidden="true">↗</span>
                  </button>
                  <time>{formatTimestamp(turn.startedAt)}</time>
                </div>
                <div className="turn-messages">
                  {turn.messages.map(message => (
                    <TurnMessageView key={`${turn.id}-${messageKey(message)}`} message={message} />
                  ))}
                </div>
              </li>
            ))}
          </ol>
        )}
      </SheetContent>
    </Sheet>
  );
}

function restoreInspectorFocus(opener: HTMLElement | null): void {
  const target = opener?.isConnected
    ? opener
    : document.querySelector<HTMLInputElement>('[name="completed-workstream-search"]');
  target?.focus();
}

function messageKey(message: TurnMessage): string {
  return JSON.stringify(message);
}

function TurnMessageView({message}: {readonly message: TurnMessage}): JSX.Element {
  if (message.kind === 'assistant')
    return (
      <article className="turn-message assistant-message">
        <span className="message-label">Agent</span>
        <p>{message.content}</p>
      </article>
    );
  if (message.kind === 'tool_call')
    return (
      <article className="turn-message tool-call-message">
        <span className="message-label">
          Tool call <b>{message.toolName}</b>
        </span>
        <pre>{JSON.stringify(message.arguments, null, 2)}</pre>
      </article>
    );
  if (message.kind === 'tool_result')
    return (
      <article
        className={`turn-message tool-result-message${message.isError ? ' message-error' : ''}`}
      >
        <span className="message-label">
          Tool result <b>{message.toolName}</b>
        </span>
        <p>{message.content}</p>
      </article>
    );
  return (
    <article className={`turn-message result-message result-${message.disposition}`}>
      <span className="message-label">Result · {message.disposition}</span>
      <p>{message.content}</p>
    </article>
  );
}

function bestMeasurement(
  scenario: CampaignRecord,
  metricId: string,
  throughSequence: number,
): CampaignRecord['measurements'][number] | undefined {
  const metric = scenario.objective.metrics.find(item => item.id === metricId);
  return [...scenario.measurements]
    .filter(
      item =>
        item.sequence <= throughSequence &&
        item.disposition === 'accepted' &&
        item.values.some(value => value.metricId === metricId),
    )
    .sort((a, b) => {
      const aValue = a.values.find(value => value.metricId === metricId)?.value ?? 0;
      const bValue = b.values.find(value => value.metricId === metricId)?.value ?? 0;
      return metric?.direction === 'minimize' ? aValue - bValue : bValue - aValue;
    })[0];
}

function workstreamName(scenario: CampaignRecord, id: string | undefined): string {
  return scenario.workstreams.find(item => item.id === id)?.title ?? 'Unknown workstream';
}

function axisTicks(domain: {min: number; max: number}): number[] {
  return [domain.max, (domain.min + domain.max) / 2, domain.min];
}

function metricName(scenario: CampaignRecord, id: string): string {
  return scenario.objective.metrics.find(item => item.id === id)?.name ?? id;
}

function agentName(scenario: CampaignRecord, id: string | null | undefined): string {
  return scenario.agents.find(item => item.id === id)?.name ?? 'Unknown agent';
}

function formatMetric(value: number | undefined, unit = ''): string {
  if (value === undefined) return '—';
  const digits = Math.abs(value) >= 100 ? 1 : 2;
  return `${new Intl.NumberFormat('en-US', {maximumFractionDigits: digits}).format(value)} ${unit}`.trim();
}

function formatCompact(value: number, unit = ''): string {
  return `${new Intl.NumberFormat('en-US', {notation: 'compact', maximumFractionDigits: 1}).format(value)}${unit ? ` ${unit}` : ''}`;
}

function formatTimestamp(value: string | undefined): string {
  if (value === undefined) return 'Time unavailable';
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone: 'UTC',
    timeZoneName: 'short',
  }).format(new Date(value));
}

function formatDate(value: number): string {
  return new Intl.DateTimeFormat('en-US', {month: 'short', day: 'numeric', timeZone: 'UTC'}).format(
    value,
  );
}

function formatDuration(milliseconds: number): string {
  const minutes = Math.round(milliseconds / 60_000);
  if (minutes < 60) return `${minutes}m`;
  const hours = Math.round(minutes / 60);
  if (hours < 48) return `${hours}h`;
  return `${Math.round(hours / 24)}d`;
}

function formatTokenSpend(tokens: number | undefined): string {
  if (tokens === undefined) return '—';
  return `${new Intl.NumberFormat('en-US', {notation: 'compact'}).format(tokens)} tokens`;
}
