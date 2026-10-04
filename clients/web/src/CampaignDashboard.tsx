import type {CSSProperties, JSX, KeyboardEvent} from 'react';
import type {CampaignRecord, TurnMessage} from './campaign-record.js';
import type {CampaignViewModel} from './campaign-view.js';

interface CampaignDashboardProps {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}

const CHART = {left: 58, right: 912, top: 26, bottom: 258};
const ROLE_COLORS = ['#94a3af', '#afa78f', '#86a69a', '#a99a91', '#899bad', '#a3a3a3'];

export function CampaignDashboard({scenario, campaign}: CampaignDashboardProps): JSX.Element {
  return (
    <main className="campaign-shell">
      <CampaignHeader scenario={scenario} campaign={campaign} />
      {campaign.view === 'objective' ? (
        <ObjectiveView scenario={scenario} />
      ) : (
        <CampaignOverview scenario={scenario} campaign={campaign} />
      )}
      <CampaignFooter scenario={scenario} />
      {campaign.selectedWorkstream !== null && (
        <WorkstreamDialog
          scenario={scenario}
          workstream={campaign.selectedWorkstream}
          cursorSequence={campaign.cursorSequence}
          cursorTimestamp={campaign.latestTimestamp}
          onClose={() => campaign.setSelectedWorkstreamId(null)}
          onAgent={id => {
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
          onClose={() => campaign.setSelectedAgentId(null)}
          onWorkstream={id => {
            campaign.setSelectedAgentId(null);
            campaign.setSelectedWorkstreamId(id);
          }}
        />
      )}
    </main>
  );
}

function CampaignHeader({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  const best = bestMeasurement(scenario, campaign.metricId, campaign.cursorSequence)?.values.find(
    value => value.metricId === campaign.metricId,
  )?.value;
  return (
    <>
      <header className="campaign-header">
        <div className="brand-lockup">
          <span className="brand-mark" aria-hidden="true">
            V
          </span>
          <span>
            VibeSys <i>/</i> campaign
          </span>
        </div>
        <div className="run-state">
          <span className="live-dot" /> Campaign trace <span className="state-divider">·</span>{' '}
          {campaign.status}
        </div>
        <button
          className="quiet-button"
          type="button"
          onClick={() =>
            campaign.setView(campaign.view === 'dashboard' ? 'objective' : 'dashboard')
          }
        >
          {campaign.view === 'dashboard' ? 'Objective view' : 'Dashboard view'}
        </button>
      </header>
      <section className="hero-row">
        <div>
          <p className="eyebrow">
            CAMPAIGN TRACE <span className="eyebrow-dot">/</span> {scenario.id}
          </p>
          <h1>{scenario.title}</h1>
          <p className="hero-summary">{scenario.summary}</p>
        </div>
        <div className="hero-kpi">
          <span className="kpi-label">BEST {campaign.metric?.name.toUpperCase()}</span>
          <strong>{formatMetric(best, campaign.metric?.unit)}</strong>
          <span className="kpi-foot">
            {scenario.objective.target === null
              ? 'No numeric target declared'
              : `Target ${formatMetric(scenario.objective.target.value, scenario.objective.target.unit)}`}
          </span>
        </div>
      </section>
      <nav className="view-tabs" aria-label="Campaign views">
        <button
          type="button"
          className={campaign.view === 'dashboard' ? 'view-tab selected' : 'view-tab'}
          onClick={() => campaign.setView('dashboard')}
        >
          Dashboard
        </button>
        <button
          type="button"
          className={campaign.view === 'objective' ? 'view-tab selected' : 'view-tab'}
          onClick={() => campaign.setView('objective')}
        >
          Objective &amp; gates
        </button>
      </nav>
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
  return (
    <>
      <PerformancePanel scenario={scenario} campaign={campaign} />
      <TimelineSection scenario={scenario} campaign={campaign} />
      <WorkstreamsSection scenario={scenario} campaign={campaign} />
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
  return (
    <section className="panel performance-panel" aria-labelledby="performance-title">
      <div className="section-head performance-head">
        <div>
          <p className="section-kicker">
            MEASUREMENTS <span>·</span> {campaign.orderedMeasurements.length} recorded
          </p>
          <h2 id="performance-title">Performance trajectory</h2>
        </div>
        <span className="metric-readout">
          {campaign.metric?.name} <i>·</i> {campaign.metric?.unit}
        </span>
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
      <MeasurementSummary scenario={scenario} campaign={campaign} />
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
    <div className="selected-measurement" aria-live="polite">
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
        {measurement?.triggeredByAgentId === null || measurement === undefined ? (
          <span>Trigger agent not recorded</span>
        ) : (
          <button
            type="button"
            onClick={() => campaign.setSelectedAgentId(measurement.triggeredByAgentId)}
          >
            Triggered by {agentName(scenario, measurement.triggeredByAgentId)}
          </button>
        )}
        {measurement?.runnerAgentId === null || measurement === undefined ? (
          <span>Runner not recorded</span>
        ) : (
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
    </div>
  );
}

function TimelineSection({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  return (
    <section className="section-block timeline-section" aria-labelledby="timeline-title">
      <div className="section-head timeline-head">
        <div>
          <p className="section-kicker">
            CONCURRENT WORK <span>·</span> {campaign.visibleWorkstreamCount} workstreams
          </p>
          <h2 id="timeline-title">
            {campaign.timelineMode === 'workstreams'
              ? 'Workstream timeline'
              : 'Agent activity timeline'}
          </h2>
        </div>
        <div className="timeline-tools">
          <fieldset className="segmented-control">
            <legend className="sr-only">Timeline mode</legend>
            <button
              type="button"
              className={campaign.timelineMode === 'workstreams' ? 'selected' : ''}
              onClick={() => campaign.setTimelineMode('workstreams')}
            >
              Workstreams
            </button>
            <button
              type="button"
              className={campaign.timelineMode === 'agents' ? 'selected' : ''}
              onClick={() => campaign.setTimelineMode('agents')}
            >
              Agent activity
            </button>
          </fieldset>
          {campaign.timelineMode === 'workstreams' && (
            <div className="timeline-key">
              <span>
                <i className="key-active" /> Active
              </span>
              <span>
                <i className="key-done" /> Accepted
              </span>
              <span>
                <i className="key-rejected" /> Rejected
              </span>
            </div>
          )}
        </div>
      </div>
      {campaign.timelineMode === 'workstreams' ? (
        <WorkstreamTimeline
          scenario={scenario}
          bounds={campaign.timeline}
          cursor={campaign.latestTimestamp}
          onWorkstream={campaign.setSelectedWorkstreamId}
        />
      ) : (
        <AgentTimeline
          scenario={scenario}
          bounds={campaign.timeline}
          cursor={campaign.latestTimestamp}
          onAgent={campaign.setSelectedAgentId}
        />
      )}
    </section>
  );
}

function WorkstreamsSection({
  scenario,
  campaign,
}: {
  readonly scenario: CampaignRecord;
  readonly campaign: CampaignViewModel;
}): JSX.Element {
  return (
    <section className="section-block workstreams-section" aria-labelledby="workstreams-title">
      <div className="section-head">
        <div>
          <p className="section-kicker">
            THE SEARCH <span>·</span> Active and completed
          </p>
          <h2 id="workstreams-title">Workstreams</h2>
        </div>
        <div className="workstream-tools">
          <fieldset className="segmented-control">
            <legend className="sr-only">Workstream view</legend>
            <button
              type="button"
              className={campaign.workstreamLayout === 'kanban' ? 'selected' : ''}
              onClick={() => campaign.setWorkstreamLayout('kanban')}
            >
              Kanban
            </button>
            <button
              type="button"
              className={campaign.workstreamLayout === 'table' ? 'selected' : ''}
              onClick={() => campaign.setWorkstreamLayout('table')}
            >
              Table
            </button>
          </fieldset>
          {campaign.workstreamLayout === 'kanban' && (
            <label className="workstream-sort">
              <span>Sort</span>
              <select
                aria-label="Sort workstreams"
                value={campaign.workstreamSort}
                onChange={event =>
                  campaign.setWorkstreamSort(
                    event.target.value as CampaignViewModel['workstreamSort'],
                  )
                }
              >
                <option value="start-asc">Start, earliest</option>
                <option value="start-desc">Start, latest</option>
                <option value="end-asc">End, earliest</option>
                <option value="end-desc">End, latest</option>
                <option value="duration-desc">Duration, longest</option>
                <option value="duration-asc">Duration, shortest</option>
                <option
                  value="tokens-desc"
                  disabled={Object.keys(campaign.workstreamTokenSpend).length === 0}
                >
                  Token spend, highest
                </option>
                <option
                  value="tokens-asc"
                  disabled={Object.keys(campaign.workstreamTokenSpend).length === 0}
                >
                  Token spend, lowest
                </option>
              </select>
            </label>
          )}
          {Object.keys(campaign.workstreamTokenSpend).length === 0 && (
            <span className="usage-note">Token usage not recorded</span>
          )}
        </div>
      </div>
      <WorkstreamExplorer
        scenario={scenario}
        asOf={campaign.latestTimestamp}
        layout={campaign.workstreamLayout}
        sort={campaign.workstreamSort}
        tokenSpend={campaign.workstreamTokenSpend}
        onSort={campaign.setWorkstreamSort}
        onSelect={campaign.setSelectedWorkstreamId}
      />
    </section>
  );
}

function CampaignFooter({scenario}: {readonly scenario: CampaignRecord}): JSX.Element {
  return (
    <footer className="campaign-footer">
      <span>
        Campaign record <i>·</i> {scenario.provenance}
      </span>
      <span>Benchmark definitions changed during this campaign</span>
    </footer>
  );
}

function ObjectiveView({scenario}: {readonly scenario: CampaignRecord}): JSX.Element {
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
  readonly scenario: CampaignRecord;
  readonly measurements: CampaignRecord['measurements'];
  readonly throughIndex: number;
  readonly metricId: string;
  readonly selectedIndex: number;
  readonly onSelect: (index: number) => void;
}

function chartModel({
  scenario,
  measurements,
  throughIndex,
  metricId,
  selectedIndex,
}: PerformanceChartProps) {
  const metric = scenario.objective.metrics.find(item => item.id === metricId);
  const points = measurements
    .map((measurement, index) => ({measurement, index}))
    .filter(
      item =>
        item.index <= throughIndex &&
        item.measurement.values.some(value => value.metricId === metricId),
    );
  const values = measurements
    .map(measurement => measurement.values.find(value => value.metricId === metricId)?.value)
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
  return {domain, metric, points, selected, selectedValue, x, y};
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
  return (
    <svg
      className="performance-chart"
      viewBox="0 0 960 300"
      role="img"
      aria-label={`${metric?.name ?? 'Performance'} measurements by campaign order. Select a point for details.`}
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
      <ChartSeries props={props} />
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
  );
}

function ChartSeries({
  props,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
}): JSX.Element {
  const path = props.model.points
    .map(({measurement, index}, pathIndex) => {
      const value = measurement.values.find(item => item.metricId === props.metricId)?.value ?? 0;
      return `${pathIndex === 0 ? 'M' : 'L'} ${props.model.x(index)} ${props.model.y(value)}`;
    })
    .join(' ');
  return (
    <g>
      <path className="performance-line" d={path} />
      {props.model.points.map(({measurement, index}) => (
        <ChartPoint key={measurement.id} props={props} measurement={measurement} index={index} />
      ))}
    </g>
  );
}

function ChartPoint({
  props,
  measurement,
  index,
}: {
  readonly props: PerformanceChartProps & {readonly model: ChartModel};
  readonly measurement: CampaignRecord['measurements'][number];
  readonly index: number;
}): JSX.Element | null {
  const value = measurement.values.find(item => item.metricId === props.metricId)?.value;
  if (value === undefined) return null;
  const cx = props.model.x(index),
    cy = props.model.y(value);
  const label = `${measurement.label}, ${formatMetric(value, props.model.metric?.unit)}, ${measurement.disposition}. Select measurement.`;
  return (
    <g className="chart-point-group">
      <circle
        className={`chart-point point-${measurement.disposition}${index === props.selectedIndex ? ' point-selected' : ''}`}
        cx={cx}
        cy={cy}
        r={index === props.selectedIndex ? 6 : 3}
      />
      <foreignObject x={cx - 4} y={cy - 4} width="8" height="8">
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

function WorkstreamTimeline({
  scenario,
  bounds,
  cursor,
  onWorkstream,
}: {
  readonly scenario: CampaignRecord;
  readonly bounds: {start: number; end: number};
  readonly cursor: string;
  readonly onWorkstream: (id: string) => void;
}): JSX.Element {
  const cursorTime = Date.parse(cursor);
  const workstreams = scenario.workstreams.filter(
    workstream => Date.parse(workstream.startedAt) <= cursorTime,
  );
  const lanes = packWorkstreams(workstreams, cursorTime);
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
      <div className="timeline-plot">
        {lanes.map(lane => (
          <div className="timeline-packed-lane" key={lane.map(item => item.id).join('|')}>
            {lane.map(workstream => {
              const barEnd = Math.min(Date.parse(workstream.finishedAt), cursorTime);
              const state =
                barEnd < Date.parse(workstream.finishedAt) ? 'active' : workstream.outcome;
              const left = position(workstream.startedAt);
              const width = Math.max(0.8, position(new Date(barEnd).toISOString()) - left);
              return (
                <button
                  type="button"
                  className={`timeline-bar outcome-${state}`}
                  key={workstream.id}
                  style={{left: `${left}%`, width: `${width}%`}}
                  aria-label={`${workstream.title}, ${state}. Open workstream.`}
                  title={`${workstream.title} · ${state}`}
                  onClick={() => onWorkstream(workstream.id)}
                >
                  {width >= 9 && <span className="timeline-bar-label">{workstream.title}</span>}
                </button>
              );
            })}
          </div>
        ))}
        <div className="cursor-line" style={{left: `${position(cursor)}%`}} aria-hidden="true" />
      </div>
      <p className="timeline-caption">
        {workstreams.length} workstreams packed into {lanes.length} lanes. Vertical overlap shows
        parallel work.
      </p>
    </div>
  );
}

function packWorkstreams(
  workstreams: CampaignRecord['workstreams'],
  cursorTime: number,
): CampaignRecord['workstreams'][] {
  const lanes: CampaignRecord['workstreams'][] = [];
  const laneEnds: number[] = [];
  const ordered = [...workstreams].sort(
    (left, right) => Date.parse(left.startedAt) - Date.parse(right.startedAt),
  );
  for (const workstream of ordered) {
    const start = Date.parse(workstream.startedAt);
    const end = Math.max(start + 1, Math.min(Date.parse(workstream.finishedAt), cursorTime));
    const availableLane = laneEnds.findIndex(laneEnd => laneEnd <= start);
    const laneIndex = availableLane < 0 ? lanes.length : availableLane;
    const lane = lanes[laneIndex] ?? [];
    lane.push(workstream);
    lanes[laneIndex] = lane;
    laneEnds[laneIndex] = end;
  }
  return lanes;
}

function AgentTimeline({
  scenario,
  bounds,
  cursor,
  onAgent,
}: {
  readonly scenario: CampaignRecord;
  readonly bounds: {start: number; end: number};
  readonly cursor: string;
  readonly onAgent: (id: string) => void;
}): JSX.Element {
  const cursorTime = Date.parse(cursor);
  const visibleEnd = Math.max(bounds.start + 1, Math.min(bounds.end, cursorTime));
  const span = Math.max(1, visibleEnd - bounds.start);
  const position = (time: number): number =>
    Math.max(0, Math.min(100, ((time - bounds.start) / span) * 100));
  return (
    <div className="timeline-card agent-timeline-card">
      <div className="timeline-axis agent-timeline-axis">
        <span>{formatDate(bounds.start)}</span>
        <span>{formatDate(bounds.start + span / 2)}</span>
        <span>{formatDate(visibleEnd)}</span>
      </div>
      <div className="agent-timeline">
        {scenario.agents.map((agent, index) => (
          <div className="agent-timeline-lane" key={agent.id}>
            <button type="button" onClick={() => onAgent(agent.id)}>
              <strong>{agent.name}</strong>
              <small>{agent.role}</small>
            </button>
            <div className="agent-activity-track">
              {agentActivityIntervals(scenario, agent.workstreamIds, cursorTime).map(interval => (
                <span
                  className="agent-activity-bar"
                  key={`${interval.start}-${interval.end}`}
                  style={
                    {
                      left: `${position(interval.start)}%`,
                      width: `${Math.max(0.8, position(interval.end) - position(interval.start))}%`,
                      '--agent-color': ROLE_COLORS[index % ROLE_COLORS.length],
                    } as CSSProperties
                  }
                />
              ))}
            </div>
          </div>
        ))}
        <div
          className="cursor-line agent-cursor-line"
          style={{left: `calc(160px + (100% - 160px) * ${position(cursorTime) / 100})`}}
          aria-hidden="true"
        />
      </div>
      <p className="timeline-caption">
        Activity windows are projected from each role&apos;s assigned workstreams.
      </p>
    </div>
  );
}

function agentActivityIntervals(
  scenario: CampaignRecord,
  workstreamIds: string[],
  cursorTime: number,
): {start: number; end: number}[] {
  const windows = scenario.workstreams
    .filter(item => workstreamIds.includes(item.id) && Date.parse(item.startedAt) <= cursorTime)
    .map(item => ({
      start: Date.parse(item.startedAt),
      end: Math.max(
        Date.parse(item.startedAt) + 1,
        Math.min(Date.parse(item.finishedAt), cursorTime),
      ),
    }))
    .sort((left, right) => left.start - right.start);
  const merged: {start: number; end: number}[] = [];
  for (const window of windows) {
    const last = merged.at(-1);
    if (last !== undefined && window.start <= last.end) last.end = Math.max(last.end, window.end);
    else merged.push({...window});
  }
  return merged;
}

type Workstream = CampaignRecord['workstreams'][number];
type WorkstreamState = 'active' | 'accepted' | 'rejected';

function WorkstreamExplorer({
  scenario,
  asOf,
  layout,
  sort,
  tokenSpend,
  onSort,
  onSelect,
}: {
  readonly scenario: CampaignRecord;
  readonly asOf: string;
  readonly layout: CampaignViewModel['workstreamLayout'];
  readonly sort: CampaignViewModel['workstreamSort'];
  readonly tokenSpend: CampaignViewModel['workstreamTokenSpend'];
  readonly onSort: CampaignViewModel['setWorkstreamSort'];
  readonly onSelect: (id: string) => void;
}): JSX.Element {
  const asOfTime = Date.parse(asOf);
  const visible = sortWorkstreams(
    scenario.workstreams.filter(workstream => Date.parse(workstream.startedAt) <= asOfTime),
    sort,
    tokenSpend,
    asOfTime,
  );
  return (
    <section className="workstream-explorer" aria-label="Workstream explorer">
      {layout === 'kanban' ? (
        <WorkstreamKanban
          workstreams={visible}
          asOfTime={asOfTime}
          tokenSpend={tokenSpend}
          onSelect={onSelect}
        />
      ) : (
        <WorkstreamTable
          workstreams={visible}
          asOfTime={asOfTime}
          tokenSpend={tokenSpend}
          sort={sort}
          onSort={onSort}
          onSelect={onSelect}
        />
      )}
    </section>
  );
}

function WorkstreamKanban({
  workstreams,
  asOfTime,
  tokenSpend,
  onSelect,
}: {
  readonly workstreams: Workstream[];
  readonly asOfTime: number;
  readonly tokenSpend: Readonly<Record<string, number>>;
  readonly onSelect: (id: string) => void;
}): JSX.Element {
  const columns: {state: WorkstreamState; label: string}[] = [
    {state: 'active', label: 'Active'},
    {state: 'accepted', label: 'Accepted'},
    {state: 'rejected', label: 'Rejected'},
  ];
  return (
    <div className="workstream-kanban">
      {columns.map(column => {
        const items = workstreams.filter(item => workstreamState(item, asOfTime) === column.state);
        return (
          <section className="kanban-column" key={column.state} aria-label={column.label}>
            <h3>
              {column.label} <span>{items.length}</span>
            </h3>
            <div className="kanban-items">
              {items.length === 0 && <p>No workstreams</p>}
              {items.map(item => (
                <button type="button" key={item.id} onClick={() => onSelect(item.id)}>
                  <strong>{item.title}</strong>
                  <span>{formatDuration(workstreamDuration(item, asOfTime))}</span>
                  {tokenSpend[item.id] !== undefined && (
                    <small>{formatTokenSpend(tokenSpend[item.id])}</small>
                  )}
                </button>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function WorkstreamTable({
  workstreams,
  asOfTime,
  tokenSpend,
  sort,
  onSort,
  onSelect,
}: {
  readonly workstreams: Workstream[];
  readonly asOfTime: number;
  readonly tokenSpend: Readonly<Record<string, number>>;
  readonly sort: CampaignViewModel['workstreamSort'];
  readonly onSort: CampaignViewModel['setWorkstreamSort'];
  readonly onSelect: (id: string) => void;
}): JSX.Element {
  return (
    <table className="workstream-table">
      <thead>
        <tr>
          <th>State</th>
          <th>Workstream</th>
          <SortableHeader label="Started" field="start" sort={sort} onSort={onSort} />
          <SortableHeader label="Ended" field="end" sort={sort} onSort={onSort} />
          <SortableHeader label="Elapsed" field="duration" sort={sort} onSort={onSort} />
          <SortableHeader
            label="Tokens"
            field="tokens"
            sort={sort}
            onSort={onSort}
            disabled={Object.keys(tokenSpend).length === 0}
          />
        </tr>
      </thead>
      <tbody>
        {workstreams.map(item => {
          const state = workstreamState(item, asOfTime);
          return (
            <tr key={item.id}>
              <td>
                <span className={`outcome-chip chip-${state}`}>{state}</span>
              </td>
              <td>
                <button type="button" onClick={() => onSelect(item.id)}>
                  {item.title}
                </button>
              </td>
              <td>{formatTimestamp(item.startedAt)}</td>
              <td>{state === 'active' ? 'In progress' : formatTimestamp(item.finishedAt)}</td>
              <td>{formatDuration(workstreamDuration(item, asOfTime))}</td>
              <td>
                <span title={tokenSpend[item.id] === undefined ? 'Tokens not recorded' : undefined}>
                  {formatTokenSpend(tokenSpend[item.id])}
                </span>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}

function SortableHeader({
  label,
  field,
  sort,
  onSort,
  disabled = false,
}: {
  readonly label: string;
  readonly field: 'start' | 'end' | 'duration' | 'tokens';
  readonly sort: CampaignViewModel['workstreamSort'];
  readonly onSort: CampaignViewModel['setWorkstreamSort'];
  readonly disabled?: boolean;
}): JSX.Element {
  const active = sort.startsWith(field);
  const ascending = active && sort.endsWith('-asc');
  return (
    <th aria-sort={active ? (ascending ? 'ascending' : 'descending') : 'none'}>
      <button
        type="button"
        disabled={disabled}
        title={disabled ? `${label} not recorded` : `Sort by ${label.toLowerCase()}`}
        onClick={() => onSort(nextTableSort(field, sort))}
      >
        {label} <span aria-hidden="true">{active ? (ascending ? '↑' : '↓') : '↕'}</span>
      </button>
    </th>
  );
}

function nextTableSort(
  field: 'start' | 'end' | 'duration' | 'tokens',
  current: CampaignViewModel['workstreamSort'],
): CampaignViewModel['workstreamSort'] {
  if (current === `${field}-asc`) return `${field}-desc`;
  if (current === `${field}-desc`) return `${field}-asc`;
  return field === 'duration' || field === 'tokens' ? `${field}-desc` : `${field}-asc`;
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
  onClose,
  onAgent,
}: {
  readonly scenario: CampaignRecord;
  readonly workstream: CampaignRecord['workstreams'][number];
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
  readonly scenario: CampaignRecord;
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

function versionLabel(scenario: CampaignRecord, id: string): string {
  return scenario.benchmarkVersions.find(item => item.id === id)?.label ?? id;
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
