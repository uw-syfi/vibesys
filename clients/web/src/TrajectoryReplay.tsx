import {
  type CSSProperties,
  type JSX,
  type KeyboardEvent,
  useEffect,
  useMemo,
  useState,
} from 'react';
import type {ReplayScenario, TurnMessage} from './replay-scenario.js';

type ReplayView = 'dashboard' | 'objective';

interface TrajectoryReplayProps {
  readonly scenario: ReplayScenario;
}

const CHART = {left: 58, right: 912, top: 26, bottom: 258};
const ROLE_COLORS = ['#84aaff', '#b59aff', '#54d6b1', '#f3ba70', '#ed8aa1', '#79c3ef'];

function latestMeasurementIndex(
  measurements: ReplayScenario['measurements'],
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

function useReplayController(scenario: ReplayScenario) {
  const [view, setView] = useState<ReplayView>('dashboard');
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
    }, 900);
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
    timeline,
    view,
    visibleWorkstreamCount,
  };
}

type ReplayController = ReturnType<typeof useReplayController>;

export function TrajectoryReplay({scenario}: TrajectoryReplayProps): JSX.Element {
  const replay = useReplayController(scenario);

  return (
    <main className="replay-shell">
      <ReplayHeader scenario={scenario} replay={replay} />
      {replay.view === 'objective' ? (
        <ObjectiveView scenario={scenario} />
      ) : (
        <ReplayDashboard scenario={scenario} replay={replay} />
      )}
      <ReplayFooter scenario={scenario} />
      {replay.selectedWorkstream !== null && (
        <WorkstreamDialog
          scenario={scenario}
          workstream={replay.selectedWorkstream}
          cursorSequence={replay.cursorSequence}
          cursorTimestamp={replay.latestTimestamp}
          onClose={() => replay.setSelectedWorkstreamId(null)}
          onAgent={id => {
            replay.setSelectedWorkstreamId(null);
            replay.setSelectedAgentId(id);
          }}
        />
      )}
      {replay.selectedAgent !== null && (
        <AgentDialog
          scenario={scenario}
          agentId={replay.selectedAgent.id}
          cursorSequence={replay.cursorSequence}
          cursorTimestamp={replay.latestTimestamp}
          onClose={() => replay.setSelectedAgentId(null)}
          onWorkstream={id => {
            replay.setSelectedAgentId(null);
            replay.setSelectedWorkstreamId(id);
          }}
        />
      )}
    </main>
  );
}

function ReplayHeader({
  scenario,
  replay,
}: {
  readonly scenario: ReplayScenario;
  readonly replay: ReplayController;
}): JSX.Element {
  const best = bestMeasurement(scenario, replay.metricId, replay.cursorSequence)?.values.find(
    value => value.metricId === replay.metricId,
  )?.value;
  return (
    <>
      <header className="replay-header">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            V
          </span>
          <span>
            VibeSys <i>/</i> replay
          </span>
        </div>
        <div className="run-state">
          <span className="live-dot" /> Fixture replay <span className="state-divider">·</span>{' '}
          Completed campaign
        </div>
        <button
          className="quiet-button"
          type="button"
          onClick={() => replay.setView(replay.view === 'dashboard' ? 'objective' : 'dashboard')}
        >
          {replay.view === 'dashboard' ? 'Objective view' : 'Dashboard view'}
        </button>
      </header>
      <section className="hero-row">
        <div>
          <p className="eyebrow">
            CAMPAIGN REPLAY <span className="eyebrow-dot">/</span> {scenario.id}
          </p>
          <h1>{scenario.title}</h1>
          <p className="hero-summary">{scenario.summary}</p>
        </div>
        <div className="hero-kpi">
          <span className="kpi-label">BEST {replay.metric?.name.toUpperCase()}</span>
          <strong>{formatMetric(best, replay.metric?.unit)}</strong>
          <span className="kpi-foot">
            {scenario.objective.target === null
              ? 'No numeric target declared'
              : `Target ${formatMetric(scenario.objective.target.value, scenario.objective.target.unit)}`}
          </span>
        </div>
      </section>
      <nav className="view-tabs" aria-label="Replay views">
        <button
          type="button"
          className={replay.view === 'dashboard' ? 'view-tab selected' : 'view-tab'}
          onClick={() => replay.setView('dashboard')}
        >
          Dashboard
        </button>
        <button
          type="button"
          className={replay.view === 'objective' ? 'view-tab selected' : 'view-tab'}
          onClick={() => replay.setView('objective')}
        >
          Objective &amp; gates
        </button>
      </nav>
    </>
  );
}

function ReplayDashboard({
  scenario,
  replay,
}: {
  readonly scenario: ReplayScenario;
  readonly replay: ReplayController;
}): JSX.Element {
  return (
    <>
      <PerformancePanel scenario={scenario} replay={replay} />
      <TimelineSection scenario={scenario} replay={replay} />
      <WorkstreamsSection scenario={scenario} replay={replay} />
    </>
  );
}

function PerformancePanel({
  scenario,
  replay,
}: {
  readonly scenario: ReplayScenario;
  readonly replay: ReplayController;
}): JSX.Element {
  return (
    <section className="panel performance-panel" aria-labelledby="performance-title">
      <div className="section-head performance-head">
        <div>
          <p className="section-kicker">
            MEASUREMENTS <span>·</span> {replay.orderedMeasurements.length} recorded
          </p>
          <h2 id="performance-title">Performance trajectory</h2>
        </div>
        <label className="metric-select-label">
          <span>Metric</span>
          <select
            value={replay.metric?.id ?? ''}
            onChange={event => replay.setMetric(event.target.value)}
            aria-label="Performance metric"
          >
            {scenario.objective.metrics.map(item => (
              <option key={item.id} value={item.id}>
                {item.name}
              </option>
            ))}
          </select>
        </label>
      </div>
      <PerformanceChart
        scenario={scenario}
        measurements={replay.orderedMeasurements}
        throughIndex={replay.pointIndex}
        metricId={replay.metric?.id ?? ''}
        selectedIndex={replay.pointIndex}
        onSelect={replay.setPointIndex}
      />
      <ReplayControls replay={replay} />
      <MeasurementSummary scenario={scenario} replay={replay} />
    </section>
  );
}

function ReplayControls({replay}: {readonly replay: ReplayController}): JSX.Element {
  const move = (offset: number): void => {
    replay.setPlaying(false);
    replay.setPointIndex(replay.pointIndex + offset);
  };
  return (
    <div className="replay-controls">
      <div className="playback-buttons">
        <button type="button" aria-label="Previous measurement" onClick={() => move(-1)}>
          ‹
        </button>
        <button
          type="button"
          className="play-button"
          aria-label={replay.playing ? 'Pause replay' : 'Play replay'}
          onClick={() => replay.setPlaying(!replay.playing)}
        >
          {replay.playing ? 'Ⅱ' : '▶'}
        </button>
        <button type="button" aria-label="Next measurement" onClick={() => move(1)}>
          ›
        </button>
      </div>
      <label className="scrubber">
        <span className="sr-only">Replay measurement</span>
        <input
          type="range"
          min="0"
          max={Math.max(0, replay.orderedMeasurements.length - 1)}
          value={replay.pointIndex}
          onChange={event => {
            replay.setPlaying(false);
            replay.setPointIndex(Number(event.target.value));
          }}
        />
      </label>
      <span className="scrubber-count">
        {String(replay.pointIndex + 1).padStart(2, '0')} <i>/</i>{' '}
        {String(replay.orderedMeasurements.length).padStart(2, '0')}
      </span>
    </div>
  );
}

function MeasurementSummary({
  scenario,
  replay,
}: {
  readonly scenario: ReplayScenario;
  readonly replay: ReplayController;
}): JSX.Element {
  const measurement = replay.activeMeasurement;
  return (
    <div className="selected-measurement" aria-live="polite">
      <div className="selected-measurement-main">
        <span
          className={`disposition-dot disposition-${measurement?.disposition ?? 'inconclusive'}`}
        />
        <div>
          <strong>{workstreamName(scenario, measurement?.workstreamId)}</strong>
          <span>
            {measurement?.benchmarkVersion} <i>·</i> {formatTimestamp(measurement?.timestamp)}
          </span>
        </div>
      </div>
      <div className="selected-measurement-detail">
        <button
          type="button"
          onClick={() => measurement && replay.setSelectedAgentId(measurement.triggeredByAgentId)}
        >
          Triggered by {agentName(scenario, measurement?.triggeredByAgentId)}
        </button>
        <button
          type="button"
          onClick={() => measurement && replay.setSelectedAgentId(measurement.runnerAgentId)}
        >
          Run by {agentName(scenario, measurement?.runnerAgentId)}
        </button>
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
        onClick={() => measurement && replay.setSelectedWorkstreamId(measurement.workstreamId)}
      >
        Inspect workstream <span aria-hidden="true">↗</span>
      </button>
    </div>
  );
}

function TimelineSection({
  scenario,
  replay,
}: {
  readonly scenario: ReplayScenario;
  readonly replay: ReplayController;
}): JSX.Element {
  return (
    <section className="section-block timeline-section" aria-labelledby="timeline-title">
      <div className="section-head timeline-head">
        <div>
          <p className="section-kicker">
            CONCURRENT WORK <span>·</span> {replay.visibleWorkstreamCount} workstreams
          </p>
          <h2 id="timeline-title">Workstream timeline</h2>
        </div>
        <div className="timeline-key">
          <span>
            <i className="key-active" /> In progress at cursor
          </span>
          <span>
            <i className="key-done" /> Completed
          </span>
        </div>
      </div>
      <Timeline
        scenario={scenario}
        bounds={replay.timeline}
        cursor={replay.latestTimestamp}
        onWorkstream={replay.setSelectedWorkstreamId}
        onAgent={replay.setSelectedAgentId}
      />
    </section>
  );
}

function WorkstreamsSection({
  scenario,
  replay,
}: {
  readonly scenario: ReplayScenario;
  readonly replay: ReplayController;
}): JSX.Element {
  return (
    <section className="section-block workstreams-section" aria-labelledby="workstreams-title">
      <div className="section-head">
        <div>
          <p className="section-kicker">
            THE SEARCH <span>·</span> Accepted and rejected
          </p>
          <h2 id="workstreams-title">Workstreams</h2>
        </div>
        <span className="section-count">{replay.visibleWorkstreamCount} visible</span>
      </div>
      <WorkstreamGrid
        scenario={scenario}
        asOf={replay.latestTimestamp}
        onSelect={replay.setSelectedWorkstreamId}
      />
    </section>
  );
}

function ReplayFooter({scenario}: {readonly scenario: ReplayScenario}): JSX.Element {
  return (
    <footer className="replay-footer">
      <span>
        Fixture replay <i>·</i> {scenario.provenance}
      </span>
      <span>Benchmark definitions changed during this campaign</span>
    </footer>
  );
}

function ObjectiveView({scenario}: {readonly scenario: ReplayScenario}): JSX.Element {
  return (
    <div className="objective-layout">
      <section className="panel objective-main">
        <p className="section-kicker">OPTIMIZATION OBJECTIVE</p>
        <h2>{scenario.objective.title}</h2>
        <p className="objective-statement">{scenario.objective.statement}</p>
        <div className="target-card">
          <span>Target</span>
          {scenario.objective.target === null ? (
            <strong>No numeric target declared</strong>
          ) : (
            <>
              <strong>
                {metricName(scenario, scenario.objective.target.metricId)}{' '}
                {scenario.objective.metrics.find(
                  item => item.id === scenario.objective.target?.metricId,
                )?.direction === 'minimize'
                  ? '≤'
                  : '≥'}{' '}
                {formatMetric(scenario.objective.target.value, scenario.objective.target.unit)}
              </strong>
              <small>
                Direction:{' '}
                {scenario.objective.metrics.find(
                  item => item.id === scenario.objective.target?.metricId,
                )?.direction ?? 'not specified'}
              </small>
            </>
          )}
        </div>
        <h3>Constraints</h3>
        <ul className="constraint-list">
          {scenario.objective.constraints.map(item => (
            <li key={item}>
              <span className="checkmark">✓</span>
              {item}
            </li>
          ))}
        </ul>
      </section>
      <section className="panel gates-panel">
        <p className="section-kicker">VALIDITY GATES</p>
        <h2>Every candidate must hold</h2>
        <div className="gate-list">
          {scenario.objective.gates.map(gate => (
            <article className="gate-card" key={gate.id}>
              <span className="gate-icon">✓</span>
              <div>
                <strong>{gate.label}</strong>
                <p>{gate.description}</p>
              </div>
            </article>
          ))}
        </div>
      </section>
      <section className="panel metric-definitions">
        <p className="section-kicker">MEASUREMENT CONTRACT</p>
        <h2>Metrics and benchmark versions</h2>
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
              <small>
                Defined in{' '}
                {item.benchmarkVersions
                  .map(version => versionLabel(scenario, version))
                  .join(' and ')}
              </small>
            </article>
          ))}
        </div>
        <div className="version-boundary-note">
          <span className="boundary-icon">↯</span>
          <div>
            <strong>Version boundary</strong>
            <p>
              {versionLabel(scenario, scenario.benchmarkVersionBoundary.fromVersion)} →{' '}
              {versionLabel(scenario, scenario.benchmarkVersionBoundary.toVersion)} at event{' '}
              {scenario.benchmarkVersionBoundary.afterSequence}.{' '}
              {scenario.benchmarkVersionBoundary.reason}
            </p>
          </div>
        </div>
      </section>
    </div>
  );
}

interface PerformanceChartProps {
  readonly scenario: ReplayScenario;
  readonly measurements: ReplayScenario['measurements'];
  readonly throughIndex: number;
  readonly metricId: string;
  readonly selectedIndex: number;
  readonly onSelect: (index: number) => void;
}

type ChartGroup = {
  version: ReplayScenario['benchmarkVersions'][number];
  points: {measurement: ReplayScenario['measurements'][number]; index: number}[];
};

function chartModel({
  scenario,
  measurements,
  throughIndex,
  metricId,
  selectedIndex,
}: PerformanceChartProps) {
  const metric = scenario.objective.metrics.find(item => item.id === metricId);
  const groups: ChartGroup[] = scenario.benchmarkVersions.map(version => ({
    version,
    points: measurements
      .map((measurement, index) => ({measurement, index}))
      .filter(
        item =>
          item.index <= throughIndex &&
          item.measurement.benchmarkVersion === version.id &&
          item.measurement.values.some(value => value.metricId === metricId),
      ),
  }));
  const domains = new Map(
    scenario.benchmarkVersions.map(version => {
      const groupValues = measurements
        .filter(measurement => measurement.benchmarkVersion === version.id)
        .map(measurement => measurement.values.find(value => value.metricId === metricId)?.value)
        .filter((value): value is number => value !== undefined);
      let min = Math.min(...groupValues);
      let max = Math.max(...groupValues);
      if (groupValues.length === 0) {
        min = 0;
        max = 1;
      }
      const padding = Math.max((max - min) * 0.08, Math.abs(max) * 0.025, 1);
      min -= padding;
      max += padding;
      return [version.id, {min, max}] as const;
    }),
  );
  const y = (value: number, versionId: string): number => {
    const domain = domains.get(versionId);
    if (domain === undefined) return CHART.bottom;
    return (
      CHART.bottom - ((value - domain.min) / (domain.max - domain.min)) * (CHART.bottom - CHART.top)
    );
  };
  const x = (index: number): number =>
    CHART.left +
    (measurements.length < 2 ? 0 : index / (measurements.length - 1)) * (CHART.right - CHART.left);
  const boundaryIndex = measurements.findIndex(
    item => item.sequence > scenario.benchmarkVersionBoundary.afterSequence,
  );
  const selected = measurements[selectedIndex];
  const selectedValue = selected?.values.find(value => value.metricId === metricId)?.value;
  return {boundaryIndex, domains, groups, metric, selected, selectedValue, x, y};
}

type ChartModel = ReturnType<typeof chartModel>;

function PerformanceChart(props: PerformanceChartProps): JSX.Element {
  const model = chartModel(props);
  return (
    <div className="chart-wrap">
      <ChartAxis scenario={props.scenario} model={model} side="left" versionIndex={0} />
      <ChartAxis scenario={props.scenario} model={model} side="right" versionIndex={1} />
      <ChartPlot {...props} model={model} />
      <div className="chart-version-legend">
        {props.scenario.benchmarkVersions.map((version, index) => (
          <span key={version.id}>
            <i className={`version-swatch swatch-${index}`} />
            {version.label}
          </span>
        ))}
      </div>
    </div>
  );
}

function ChartAxis({
  scenario,
  model,
  side,
  versionIndex,
}: {
  readonly scenario: ReplayScenario;
  readonly model: ChartModel;
  readonly side: 'left' | 'right';
  readonly versionIndex: number;
}): JSX.Element {
  const version = scenario.benchmarkVersions[versionIndex];
  return (
    <div className={`chart-y-axis chart-y-axis-${side}`} aria-hidden="true">
      <small>{version?.label ?? (side === 'left' ? 'Earlier' : 'Later')} scale</small>
      {axisTicks(version?.id, model.domains).map(tick => (
        <span key={`${version?.id ?? side}-${tick}`}>
          {formatCompact(tick, model.metric?.unit)}
        </span>
      ))}
    </div>
  );
}

function ChartPlot(props: PerformanceChartProps & {readonly model: ChartModel}): JSX.Element {
  const {metric, selected, selectedValue, x, y} = props.model;
  return (
    <svg
      className="performance-chart"
      viewBox="0 0 960 300"
      role="img"
      aria-label={`${metric?.name ?? 'Performance'} measurements by replay order. Select a point for details.`}
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
      <ChartTargets props={props} />
      {props.model.boundaryIndex > 0 && (
        <>
          <line
            className="version-boundary-line"
            x1={(x(props.model.boundaryIndex - 1) + x(props.model.boundaryIndex)) / 2}
            x2={(x(props.model.boundaryIndex - 1) + x(props.model.boundaryIndex)) / 2}
            y1={CHART.top}
            y2={CHART.bottom + 20}
          />
          <text
            className="version-boundary-label"
            x={(x(props.model.boundaryIndex - 1) + x(props.model.boundaryIndex)) / 2 + 8}
            y={CHART.top + 8}
          >
            {versionLabel(props.scenario, props.scenario.benchmarkVersionBoundary.toVersion)}
          </text>
        </>
      )}
      <ChartSeries props={props} />
      {selectedValue !== undefined && selected !== undefined && (
        <line
          className="selected-guide"
          x1={x(props.selectedIndex)}
          x2={x(props.selectedIndex)}
          y1={y(selectedValue, selected.benchmarkVersion)}
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
  );
}

function ChartTargets({
  props,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
}): JSX.Element | null {
  const target = props.scenario.objective.target;
  if (target === null || target.metricId !== props.metricId) return null;
  return (
    <>
      {props.model.groups.map(({version, points}) => {
        if (!props.model.metric?.benchmarkVersions.includes(version.id) || points.length === 0)
          return null;
        const targetY = props.model.y(target.value, version.id);
        return (
          <g key={`target-${version.id}`}>
            <line
              className="target-line"
              x1={props.model.x(points[0]?.index ?? 0)}
              x2={props.model.x(points.at(-1)?.index ?? 0)}
              y1={targetY}
              y2={targetY}
            />
            <text
              className="target-label"
              x={props.model.x(points.at(-1)?.index ?? 0) - 4}
              y={targetY - 7}
              textAnchor="end"
            >
              TARGET {formatCompact(target.value, target.unit)}
            </text>
          </g>
        );
      })}
    </>
  );
}

function ChartSeries({
  props,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
}): JSX.Element {
  return (
    <>
      {props.model.groups.map(
        ({version, points}) =>
          points.length > 0 && (
            <g key={version.id}>
              <path
                className="performance-line"
                d={points
                  .map(({measurement, index: pointIndex}, pathIndex) => {
                    const value =
                      measurement.values.find(item => item.metricId === props.metricId)?.value ?? 0;
                    return `${pathIndex === 0 ? 'M' : 'L'} ${props.model.x(pointIndex)} ${props.model.y(value, version.id)}`;
                  })
                  .join(' ')}
              />
              {points.map(({measurement, index}) => (
                <ChartPoint
                  key={measurement.id}
                  props={props}
                  measurement={measurement}
                  index={index}
                  versionId={version.id}
                />
              ))}
            </g>
          ),
      )}
    </>
  );
}

function ChartPoint({
  props,
  measurement,
  index,
  versionId,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
  readonly measurement: ReplayScenario['measurements'][number];
  readonly index: number;
  readonly versionId: string;
}): JSX.Element | null {
  const value = measurement.values.find(item => item.metricId === props.metricId)?.value;
  if (value === undefined) return null;
  const cx = props.model.x(index),
    cy = props.model.y(value, versionId);
  const label = `${workstreamName(props.scenario, measurement.workstreamId)}, ${formatMetric(value, props.model.metric?.unit)}, ${measurement.disposition}. Select measurement.`;
  return (
    <g className="chart-point-group">
      <circle
        className={`chart-point point-${measurement.disposition}${index === props.selectedIndex ? ' point-selected' : ''}`}
        cx={cx}
        cy={cy}
        r={index === props.selectedIndex ? 7 : 4.2}
      />
      <foreignObject x={cx - 12} y={cy - 12} width="24" height="24">
        <button
          type="button"
          className="chart-point-button"
          aria-label={label}
          onClick={() => props.onSelect(index)}
        />
      </foreignObject>
    </g>
  );
}

function Timeline({
  scenario,
  bounds,
  cursor,
  onWorkstream,
  onAgent,
}: {
  readonly scenario: ReplayScenario;
  readonly bounds: {start: number; end: number};
  readonly cursor: string;
  readonly onWorkstream: (id: string) => void;
  readonly onAgent: (id: string) => void;
}): JSX.Element {
  const lanes = scenario.agents.map((agent, index) => ({
    agent,
    color: ROLE_COLORS[index % ROLE_COLORS.length],
  }));
  const cursorTime = Date.parse(cursor);
  const visibleEnd = Math.max(bounds.start + 1, Math.min(bounds.end, cursorTime));
  const span = Math.max(1, visibleEnd - bounds.start);
  const position = (time: string): number =>
    Math.max(0, Math.min(100, ((Date.parse(time) - bounds.start) / span) * 100));
  return (
    <div className="timeline-card">
      <div className="timeline-axis">
        <span>{formatDate(bounds.start)}</span>
        <span>{formatDate(bounds.start + span / 2)}</span>
        <span>{formatDate(visibleEnd)}</span>
      </div>
      <div className="timeline-lanes">
        {lanes.map(({agent, color}) => {
          const workstreams = scenario.workstreams.filter(
            workstream =>
              agent.workstreamIds.includes(workstream.id) &&
              Date.parse(workstream.startedAt) <= cursorTime,
          );
          return (
            <div className="timeline-lane" key={agent.id}>
              <button type="button" className="lane-agent" onClick={() => onAgent(agent.id)}>
                <span className="agent-avatar" style={{'--agent-color': color} as CSSProperties}>
                  {initials(agent.name)}
                </span>
                <span>
                  <strong>{agent.name}</strong>
                  <small>{agent.role}</small>
                </span>
              </button>
              <div className="lane-track">
                <div className="lane-rows">
                  {workstreams.map((workstream, index) => {
                    const visibleEnd = Math.min(Date.parse(workstream.finishedAt), cursorTime);
                    const state =
                      visibleEnd < Date.parse(workstream.finishedAt)
                        ? 'active'
                        : workstream.outcome;
                    return (
                      <button
                        type="button"
                        className={`timeline-bar outcome-${state}`}
                        key={workstream.id}
                        style={
                          {
                            left: `${position(workstream.startedAt)}%`,
                            width: `${Math.max(0.8, position(new Date(visibleEnd).toISOString()) - position(workstream.startedAt))}%`,
                            top: `${8 + (index % 2) * 13}px`,
                            '--agent-color': color,
                          } as CSSProperties
                        }
                        aria-label={`${workstream.title}, ${state}. Open workstream.`}
                        title={`${workstream.title} · ${state}`}
                        onClick={() => onWorkstream(workstream.id)}
                      >
                        <span />
                      </button>
                    );
                  })}
                </div>
                <div
                  className="cursor-line"
                  style={{left: `${position(cursor)}%`}}
                  aria-hidden="true"
                >
                  <span />
                </div>
              </div>
            </div>
          );
        })}
      </div>
      <p className="timeline-caption">
        Overlapping bars show concurrent ownership. Select a role to inspect its recorded turns.
      </p>
    </div>
  );
}

function WorkstreamGrid({
  scenario,
  asOf,
  onSelect,
}: {
  readonly scenario: ReplayScenario;
  readonly asOf: string;
  readonly onSelect: (id: string) => void;
}): JSX.Element {
  const asOfTime = Date.parse(asOf);
  const visible = scenario.workstreams
    .filter(workstream => Date.parse(workstream.startedAt) <= asOfTime)
    .sort((left, right) => {
      const leftActive = Date.parse(left.finishedAt) > asOfTime;
      const rightActive = Date.parse(right.finishedAt) > asOfTime;
      if (leftActive !== rightActive) return leftActive ? -1 : 1;
      if (!leftActive && left.outcome !== right.outcome)
        return left.outcome === 'accepted' ? -1 : 1;
      return Date.parse(left.startedAt) - Date.parse(right.startedAt);
    });
  return (
    <div className="workstream-grid">
      {visible.map((workstream, index) => {
        const finished = Date.parse(workstream.finishedAt) <= asOfTime;
        const state = !finished
          ? 'Active'
          : workstream.outcome === 'accepted'
            ? 'Accepted'
            : 'Rejected';
        const agents = scenario.agents.filter(agent => agent.workstreamIds.includes(workstream.id));
        return (
          <button
            type="button"
            className="workstream-card"
            key={workstream.id}
            onClick={() => onSelect(workstream.id)}
          >
            <span className="card-topline">
              <span className="workstream-number">{String(index + 1).padStart(2, '0')}</span>
              <span className={`outcome-chip chip-${state.toLowerCase()}`}>{state}</span>
            </span>
            <strong>{workstream.title}</strong>
            <span className="workstream-hypothesis">{workstream.hypothesis}</span>
            <span className="workstream-card-bottom">
              <span>{agents.map(agent => initials(agent.name)).join(' · ')}</span>
              <span>
                Open <i aria-hidden="true">↗</i>
              </span>
            </span>
          </button>
        );
      })}
    </div>
  );
}

function WorkstreamDialog({
  scenario,
  workstream,
  cursorSequence,
  cursorTimestamp,
  onClose,
  onAgent,
}: {
  readonly scenario: ReplayScenario;
  readonly workstream: ReplayScenario['workstreams'][number];
  readonly cursorSequence: number;
  readonly cursorTimestamp: string;
  readonly onClose: () => void;
  readonly onAgent: (id: string) => void;
}): JSX.Element {
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
  const closeOnEscape = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key === 'Escape') onClose();
  };
  return (
    <div className="dialog-scrim">
      <section
        className="detail-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="workstream-dialog-title"
        onKeyDown={closeOnEscape}
      >
        <div className="dialog-head">
          <div>
            <p className="section-kicker">
              WORKSTREAM DETAIL <span>·</span> {workstream.id}
            </p>
            <h2 id="workstream-dialog-title">{workstream.title}</h2>
          </div>
          <button
            type="button"
            className="dialog-close"
            aria-label="Close workstream detail"
            onClick={onClose}
          >
            ×
          </button>
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
              : 'This workstream is in progress at the selected replay position.'}
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
          Agent turn share <small>based on recorded turn counts</small>
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
                  onClick={() => onAgent(agent.id)}
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
                        {agent.role} · {count} turns
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
      </section>
    </div>
  );
}

function AgentDialog({
  scenario,
  agentId,
  cursorSequence,
  cursorTimestamp,
  onClose,
  onWorkstream,
}: {
  readonly scenario: ReplayScenario;
  readonly agentId: string;
  readonly cursorSequence: number;
  readonly cursorTimestamp: string;
  readonly onClose: () => void;
  readonly onWorkstream: (id: string) => void;
}): JSX.Element | null {
  const agent = scenario.agents.find(item => item.id === agentId);
  const turns = (scenario.trajectories.find(item => item.agentId === agentId)?.turns ?? []).filter(
    turn =>
      Date.parse(turn.startedAt) <= Date.parse(cursorTimestamp) &&
      (scenario.workstreams.find(workstream => workstream.id === turn.workstreamId)
        ?.firstSequence ?? Number.POSITIVE_INFINITY) <= cursorSequence,
  );
  const closeOnEscape = (event: KeyboardEvent<HTMLElement>): void => {
    if (event.key === 'Escape') onClose();
  };
  if (agent === undefined) return null;
  return (
    <div className="dialog-scrim">
      <section
        className="detail-dialog trajectory-dialog"
        role="dialog"
        aria-modal="true"
        aria-labelledby="agent-dialog-title"
        onKeyDown={closeOnEscape}
      >
        <div className="dialog-head">
          <div>
            <p className="section-kicker">
              AGENT TRAJECTORY <span>·</span> {agent.role}
            </p>
            <h2 id="agent-dialog-title">{agent.name}</h2>
          </div>
          <button
            type="button"
            className="dialog-close"
            aria-label="Close agent trajectory"
            onClick={onClose}
          >
            ×
          </button>
        </div>
        <p className="dialog-hypothesis">
          {agent.role} · {turns.length} recorded turns across{' '}
          {new Set(turns.map(turn => turn.workstreamId)).size} workstreams
        </p>
        {turns.length === 0 ? (
          <p className="empty-state">No turn-level trajectory was recorded for this agent.</p>
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
      </section>
    </div>
  );
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
  scenario: ReplayScenario,
  metricId: string,
  throughSequence: number,
): ReplayScenario['measurements'][number] | undefined {
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

function timelineBounds(scenario: ReplayScenario): {start: number; end: number} {
  const start = Math.min(...scenario.workstreams.map(item => Date.parse(item.startedAt)));
  const end = Math.max(...scenario.workstreams.map(item => Date.parse(item.finishedAt)));
  return {start: Number.isFinite(start) ? start : 0, end: Number.isFinite(end) ? end : 1};
}

function workstreamName(scenario: ReplayScenario, id: string | undefined): string {
  return scenario.workstreams.find(item => item.id === id)?.title ?? 'Unknown workstream';
}

function versionLabel(scenario: ReplayScenario, id: string): string {
  return scenario.benchmarkVersions.find(item => item.id === id)?.label ?? id;
}

function axisTicks(
  versionId: string | undefined,
  domains: ReadonlyMap<string, {min: number; max: number}>,
): number[] {
  if (versionId === undefined) return [0, 0, 0];
  const domain = domains.get(versionId);
  if (domain === undefined) return [0, 0, 0];
  return [domain.max, (domain.min + domain.max) / 2, domain.min];
}

function metricName(scenario: ReplayScenario, id: string): string {
  return scenario.objective.metrics.find(item => item.id === id)?.name ?? id;
}

function agentName(scenario: ReplayScenario, id: string | undefined): string {
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

function initials(value: string): string {
  return value
    .split(/\s+/)
    .slice(0, 2)
    .map(part => part[0] ?? '')
    .join('')
    .toUpperCase();
}
