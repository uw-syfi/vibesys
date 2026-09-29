import type {DesignRound, HypothesisEntry, RunEvent} from '@vibesys/backend-client';
import {Fragment, useMemo} from 'react';
import {
  type ChartModel,
  type ChartPoint,
  chartModel,
  type DesignRow,
  designRows,
  type Evidence,
  evidenceRows,
} from '../experiments.js';
import type {RunSummary} from '../rounds.js';
import type {RoundEdits} from '../transcript.js';
import {PaneHead} from './Pane.js';

export interface ExperimentsProps {
  chart: ChartModel | null;
  evidence: Evidence[];
  design: DesignRow[];
  view: 'hypotheses' | 'design';
  /** The round whose evidence is open. */
  open: number | null;
  progress: string;
  onView: (view: 'hypotheses' | 'design') => void;
  onToggle: (round: number) => void;
  onRound: (round: number) => void;
  onChanges: (round: number) => void;
}

function PointMark({point}: {point: ChartPoint}) {
  if (point.mark === 'unmeasured') {
    return (
      <path
        d={`M${point.x - 3.5} ${point.y - 3.5}l7 7m0 -7l-7 7`}
        stroke="var(--error)"
        strokeWidth={1.5}
      >
        <title>{point.label}</title>
      </path>
    );
  }
  const fill = point.mark === 'kept' ? 'var(--text-1)' : 'var(--bg-app)';
  const stroke =
    point.mark === 'judging'
      ? 'var(--live)'
      : point.mark === 'kept'
        ? 'var(--text-1)'
        : 'var(--error)';
  return (
    <circle cx={point.x} cy={point.y} r={3.5} fill={fill} stroke={stroke} strokeWidth={1.5}>
      <title>{point.label}</title>
    </circle>
  );
}

const LEGEND = (
  <div className="legend">
    <span>
      <svg width="16" height="8" aria-hidden="true">
        <path d="M1 4h14" stroke="var(--text-1)" strokeWidth="1.5" />
      </svg>
      Retained
    </span>
    <span>
      <svg width="8" height="8" aria-hidden="true">
        <circle cx="4" cy="4" r="3" fill="var(--text-1)" />
      </svg>
      Kept
    </span>
    <span>
      <svg width="8" height="8" aria-hidden="true">
        <circle cx="4" cy="4" r="3" fill="none" stroke="var(--error)" strokeWidth="1.5" />
      </svg>
      Rejected
    </span>
    <span>
      <svg width="8" height="8" aria-hidden="true">
        <circle cx="4" cy="4" r="3" fill="none" stroke="var(--live)" strokeWidth="1.5" />
      </svg>
      Being judged
    </span>
    <span>
      <svg width="8" height="8" aria-hidden="true">
        <path d="M1 1l6 6m0-6l-6 6" stroke="var(--error)" strokeWidth="1.5" />
      </svg>
      Not measured
    </span>
    <span>
      <svg width="8" height="8" aria-hidden="true">
        <circle cx="4" cy="4" r="2" fill="none" stroke="var(--line)" />
      </svg>
      Planned
    </span>
  </div>
);

function Chart({chart}: {chart: ChartModel | null}) {
  if (chart === null) return <p className="empty1">No round has a measurement yet.</p>;
  const label = chart.width - 36;
  return (
    <>
      <div className="chart">
        <svg
          viewBox={`0 0 ${chart.width} ${chart.height}`}
          role="img"
          aria-label="Retained metric and attempts by round"
        >
          {chart.baseline === null ? null : (
            <line
              x1={6}
              x2={chart.width - 42}
              y1={chart.baseline.y}
              y2={chart.baseline.y}
              stroke="var(--line)"
            />
          )}
          {chart.path === '' ? null : (
            <path d={chart.path} fill="none" stroke="var(--text-1)" strokeWidth={1.5} />
          )}
          {chart.points.map(point => (
            <PointMark key={point.round} point={point} />
          ))}
          {chart.planned.map(point => (
            <circle
              key={`p${point.round}`}
              cx={point.x}
              cy={chart.floor}
              r={2}
              fill="none"
              stroke="var(--line)"
            />
          ))}
          {chart.ticks.map(tick => (
            <text key={`t${tick.round}`} x={tick.x} y={chart.height - 3} textAnchor="middle">
              {tick.round}
            </text>
          ))}
          {chart.baseline === null ? null : (
            <text x={label} y={chart.baseline.y + 4}>
              <title>{chart.baseline.label}</title>
              {chart.baseline.value}
            </text>
          )}
          {chart.retained === null ? null : (
            <text className="v" x={label} y={chart.retained.y + 4}>
              <title>{chart.retained.label}</title>
              {chart.retained.value}
            </text>
          )}
        </svg>
      </div>
      {LEGEND}
    </>
  );
}

function EvidenceList({
  evidence,
  open,
  onToggle,
  onRound,
  onChanges,
}: Pick<ExperimentsProps, 'evidence' | 'open' | 'onToggle' | 'onRound' | 'onChanges'>) {
  return (
    <ul className="evlist">
      {evidence.map(row => (
        <li key={row.round}>
          <button
            type="button"
            className={open === row.round ? 'xrow sel' : 'xrow'}
            aria-expanded={open === row.round}
            onClick={() => onToggle(row.round)}
          >
            <span className="id">r{row.round}</span>
            <span className="ttl">{row.title}</span>
            <span className={`oc ${row.outcome.tone}`}>{row.outcome.text}</span>
            <span className="m" title={row.valueLabel ?? 'Not measured'}>
              {row.value ?? '—'}
            </span>
          </button>
          {open === row.round ? (
            <div className="ev">
              <dl className="kv">
                {row.facts.map(fact => (
                  <Fragment key={fact.term}>
                    <dt>{fact.term}</dt>
                    <dd className={fact.mono ? 'mono' : undefined}>{fact.text}</dd>
                  </Fragment>
                ))}
              </dl>
              <div className="evacts">
                <button type="button" className="linkbtn" onClick={() => onRound(row.round)}>
                  Open round {row.round}
                </button>
                <button type="button" className="linkbtn" onClick={() => onChanges(row.round)}>
                  View changes
                </button>
              </div>
            </div>
          ) : null}
        </li>
      ))}
    </ul>
  );
}

function DesignList({design, onChanges}: Pick<ExperimentsProps, 'design' | 'onChanges'>) {
  if (design.length === 0) return <p className="empty1">No round has recorded its changes yet.</p>;
  return (
    <div>
      {design.map(row => (
        <button
          key={row.round}
          type="button"
          className="xrow design"
          onClick={() => onChanges(row.round)}
        >
          <span className="id">r{row.round}</span>
          <span>
            <div className="mono">{row.files}</div>
            {row.summary === null ? null : <div className="t2">{row.summary}</div>}
          </span>
          <span className="m">
            {row.stat === null ? null : (
              <div>
                <span className="ok">+{row.stat.added}</span>{' '}
                <span className="bad">−{row.stat.removed}</span>
              </div>
            )}
            {row.reverted ? <div className="t2">reverted</div> : null}
          </span>
        </button>
      ))}
    </div>
  );
}

export function Experiments(props: ExperimentsProps) {
  const {view, onView} = props;
  return (
    <>
      <PaneHead scope="Run">
        <span>{props.progress}</span>
        <span className="r seg2">
          <button
            type="button"
            className={view === 'hypotheses' ? 'disc on' : 'disc'}
            aria-pressed={view === 'hypotheses'}
            onClick={() => onView('hypotheses')}
          >
            Hypotheses
          </button>
          <button
            type="button"
            className={view === 'design' ? 'disc on' : 'disc'}
            aria-pressed={view === 'design'}
            onClick={() => onView('design')}
          >
            Design
          </button>
        </span>
      </PaneHead>
      {view === 'design' ? (
        <DesignList design={props.design} onChanges={props.onChanges} />
      ) : (
        <>
          <Chart chart={props.chart} />
          <EvidenceList
            evidence={props.evidence}
            open={props.open}
            onToggle={props.onToggle}
            onRound={props.onRound}
            onChanges={props.onChanges}
          />
        </>
      )}
    </>
  );
}

export interface ExperimentsTabProps
  extends Omit<ExperimentsProps, 'chart' | 'evidence' | 'design' | 'progress'> {
  summary: RunSummary;
  experiments: readonly HypothesisEntry[];
  designRounds: readonly DesignRound[];
  captured: readonly RunEvent[];
  edits: RoundEdits;
  maxRounds: number | null;
}

export function ExperimentsTab({
  summary,
  experiments,
  designRounds,
  captured,
  edits,
  maxRounds,
  ...rest
}: ExperimentsTabProps) {
  const chart = useMemo(() => chartModel(summary, maxRounds), [summary, maxRounds]);
  const evidence = useMemo(
    () => evidenceRows(summary, experiments, designRounds, captured),
    [summary, experiments, designRounds, captured],
  );
  const design = useMemo(
    () => designRows(summary, designRounds, captured, edits),
    [summary, designRounds, captured, edits],
  );
  const count = summary.rows.length;
  const progress = maxRounds === null ? `${count} rounds` : `${count} of ${maxRounds} rounds`;
  return (
    <Experiments {...rest} chart={chart} evidence={evidence} design={design} progress={progress} />
  );
}
