import {ArrowDown, ArrowUp} from 'lucide-react';
import type {SummaryModel, TrendModel} from '../model.js';
import './Summary.css';

/**
 * The run's result under the header, the first thing a reader looks for: the metric, the baseline,
 * the best kept round against it, progress through the round budget, and the metric's shape.
 */
export function Summary({model, trend}: {model: SummaryModel; trend: TrendModel | null}) {
  const {metric, unit, direction, baseline, best, done, max, kept} = model;
  return (
    <section className="sum" aria-label="Result">
      <div className="stat stat-metric">
        <p className="lbl">Optimizing</p>
        <p className="metric">
          <span className="mono">{metric}</span>
          {direction === null ? null : (
            <span
              className="dir"
              data-tip={direction === 'max' ? 'Higher is better' : 'Lower is better'}
            >
              {direction === 'max' ? (
                <ArrowUp size={14} strokeWidth={2} aria-hidden />
              ) : (
                <ArrowDown size={14} strokeWidth={2} aria-hidden />
              )}
              <span className="sr-only">
                {direction === 'max' ? ', higher is better' : ', lower is better'}
              </span>
            </span>
          )}
        </p>
      </div>
      <div className="stat">
        <p className="lbl">Baseline</p>
        <p className="num">
          {baseline ?? '—'}
          {unit === null || baseline === null ? null : <span className="unit">{unit}</span>}
        </p>
      </div>
      <div className="stat">
        <p className="lbl">
          Best{best === null ? null : <span className="at"> · R{best.round}</span>}
        </p>
        <p className="num">
          {best === null ? (
            <span className="none">
              {direction === null ? 'Direction not set' : 'No kept round yet'}
            </span>
          ) : (
            <>
              {best.value}
              {unit === null ? null : <span className="unit">{unit}</span>}
              {best.delta === null ? null : (
                <span
                  className={best.improved ? 'delta up' : 'delta down'}
                  data-tip="Against the baseline"
                >
                  {best.delta}
                </span>
              )}
            </>
          )}
        </p>
      </div>
      <div className="stat">
        <p className="lbl">Rounds</p>
        <p className="num">
          {done}
          {max === null ? null : <span className="unit">of {max}</span>}
          <span className="kept">{kept} kept</span>
        </p>
      </div>
      {trend === null ? null : <Trend model={trend} />}
    </section>
  );
}

/** One polyline and a dot on the last point; the stats beside it carry the numbers. */
function Trend({model}: {model: TrendModel}) {
  const {points, first, last, firstRound, lastRound} = model;
  const dot = points.at(-1);
  if (dot === undefined) return null;
  return (
    <div className="trend">
      <svg
        className="tplot"
        viewBox="0 0 100 36"
        preserveAspectRatio="none"
        role="img"
        aria-label={`Metric trend: ${first} to ${last}, R${firstRound} to R${lastRound}`}
      >
        <polyline points={points.map(point => `${point.x},${point.y}`).join(' ')} />
        <path className="tdot" d={`M${dot.x},${dot.y}h0`} />
      </svg>
      <p className="tends mono" aria-hidden="true">
        <span>R{firstRound}</span>
        <span>R{lastRound}</span>
      </p>
    </div>
  );
}
