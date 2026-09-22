import {roundAgentElapsedMs} from '@vibesys/core-state';
import {
  BadgeCheck,
  Circle,
  CircleCheck,
  CircleMinus,
  CirclePause,
  CircleX,
  LoaderCircle,
  type LucideIcon,
  Trophy,
} from 'lucide-react';
import {useEffect, useRef} from 'react';
import type {RailModel, RailRow, RailState, RoundStatus, TrendModel} from '../model.js';
import {Elapsed} from './Elapsed.js';
import './Rail.css';

export interface RailProps {
  state: RailState;
  model: RailModel;
  selected: number | null;
  /** The experiments query failed; shown under the rows with Retry. */
  error: string | null;
  /** Phone only, until the first sheet opens. */
  hint: boolean;
  /** The metric's shape across rounds; null below two measured points. */
  trend: TrendModel | null;
  onSelect: (round: number) => void;
  onRetry: () => void;
}

const STATUS: Record<RoundStatus, {word: string; Icon: LucideIcon}> = {
  baseline: {word: 'Baseline', Icon: Circle},
  kept: {word: 'Kept', Icon: CircleCheck},
  rejected: {word: 'Rejected', Icon: CircleMinus},
  failed: {word: 'Failed', Icon: CircleX},
  running: {word: 'Running', Icon: LoaderCircle},
  paused: {word: 'Paused', Icon: CirclePause},
};
const OFFICIAL = 'Official. Unmarked values are provisional.';
const INCUMBENT = 'Incumbent: the best result so far. New rounds are compared against it.';

export function Rail({state, model, selected, error, hint, trend, onSelect, onRetry}: RailProps) {
  const list = useRef<HTMLOListElement>(null);
  // Keyboard selection moves focus with it, and the selected row scrolls into view once the rows
  // render, and again once the fonts load (they widen the phone chips).
  useEffect(() => {
    const rows = list.current;
    if (selected === null || state !== 'ready' || rows === null) return;
    const current = rows.querySelector<HTMLElement>('[aria-current="true"]');
    if (current === null) return;
    if (rows.contains(document.activeElement)) current.focus({preventScroll: true});
    // Scroll the strip (phone) and the rail (wider) to the nearest edge by hand, never the page:
    // Chromium's scrollIntoView also moves the Tab starting point, so Tab would skip the header.
    const reveal = () => {
      for (const box of [rows, rows.parentElement]) {
        if (box === null) continue;
        const view = box.getBoundingClientRect();
        const row = current.getBoundingClientRect();
        box.scrollLeft += Math.min(0, row.left - view.left) + Math.max(0, row.right - view.right);
        box.scrollTop += Math.min(0, row.top - view.top) + Math.max(0, row.bottom - view.bottom);
      }
    };
    reveal();
    let stale = false;
    void document.fonts.ready.then(() => {
      if (!stale) reveal();
    });
    return () => {
      stale = true;
    };
  }, [selected, state]);
  // Roving tabindex: the selected row is the rail's one Tab stop.
  const stop = model.rows.some(row => row.round === selected) ? selected : model.rows[0]?.round;

  return (
    <nav className="rail" aria-label="Rounds">
      {state === 'error' ? null : state === 'loading' ? (
        <ol className="rail-rows" aria-hidden="true">
          {['a', 'b', 'c', 'd'].map(key => (
            <li key={key}>
              <span className="rrow skel" />
            </li>
          ))}
        </ol>
      ) : state === 'unattached' ? (
        <p className="rail-note">Waiting for the project to attach</p>
      ) : model.rows.length === 0 ? (
        <p className="rail-note">No rounds started yet</p>
      ) : (
        <ol className="rail-rows" ref={list}>
          {model.rows.map(row => (
            <li key={row.round}>
              <Row
                row={row}
                selected={row.round === selected}
                stop={row.round === stop}
                onSelect={onSelect}
              />
            </li>
          ))}
        </ol>
      )}
      {state === 'ready' && trend !== null ? <Trend model={trend} /> : null}
      {state === 'ready' && model.roundsLeft !== null ? (
        <p className="left">
          {model.roundsLeft}
          <span className="long">{model.roundsLeft === 1 ? ' round' : ' rounds'}</span> left
        </p>
      ) : null}
      {hint && state === 'ready' && model.rows.length > 0 ? (
        <p className="rail-hint">Tap the selected round for details</p>
      ) : null}
      {error === null ? null : (
        <p className="rail-note rail-error" role="alert">
          Could not load rounds: {error}{' '}
          <button type="button" className="btn btn-sm" onClick={onRetry}>
            Retry
          </button>
        </p>
      )}
      {state === 'loading' ? <p className="sr-only">Loading rounds</p> : null}
    </nav>
  );
}

/**
 * The shape of the metric across rounds, under the rows: one polyline and a dot on the last point.
 * The rows already carry every value, the incumbent and the provenance, so this adds no axes, no
 * labels and no tooltip, only the two ends of its own scale. The phone strip hides it (CSS).
 */
function Trend({model}: {model: TrendModel}) {
  const {points, first, last, firstRound, lastRound} = model;
  const dot = points.at(-1);
  if (dot === undefined) return null;
  return (
    <div className="trend">
      {/* The box stretches to the rail's width, so the dot is a round-capped zero-length path:
          a circle would go oval with it, while a non-scaling stroke keeps its 3px. */}
      <svg
        className="tplot"
        viewBox="0 0 100 36"
        preserveAspectRatio="none"
        role="img"
        // The span the curve covers, not a count of points: a round with no value has no point,
        // so any count would disagree with the rows above.
        aria-label={`Metric trend: ${first} to ${last}, R${firstRound} to R${lastRound}`}
      >
        <polyline points={points.map(point => `${point.x},${point.y}`).join(' ')} />
        <path className="tdot" d={`M${dot.x},${dot.y}h0`} />
      </svg>
      <p className="tends mono" aria-hidden="true">
        <span>{first}</span>
        <span>{last}</span>
      </p>
    </div>
  );
}

function Row({
  row,
  selected,
  stop,
  onSelect,
}: {
  row: RailRow;
  selected: boolean;
  stop: boolean;
  onSelect: (round: number) => void;
}) {
  const {word, Icon} = STATUS[row.status];
  const live = row.live;
  // One provenance per row, for both the badge and the name. None without a value, and none for
  // R0: the protocol does not say how the baseline was measured.
  const provenance =
    row.value === null || row.status === 'baseline'
      ? null
      : row.official
        ? 'official'
        : 'provisional';
  // The name starts with the visible text (WCAG 2.5.3) and says everything the row's tips say, as
  // one comma-separated run. The live row's elapsed stays out of it: a name that ticks every second
  // is re-announced every second while the row has focus.
  const name = [
    `R${row.round}`,
    live === null ? row.value : null,
    word.toLowerCase(),
    live === null ? row.valueTip : null,
    provenance,
    row.incumbent ? 'incumbent' : null,
  ]
    .filter(part => part !== null)
    .join(', ');
  return (
    <button
      type="button"
      className="rrow"
      tabIndex={stop ? 0 : -1}
      aria-current={selected ? 'true' : undefined}
      data-tip={live === null ? (row.valueTip ?? undefined) : 'Round elapsed'}
      data-side="right"
      onClick={() => onSelect(row.round)}
    >
      <span className="sr-only">{name}</span>
      <span className={`ico st-${row.status}`} data-tip={word} data-side="right">
        <Icon
          size={16}
          strokeWidth={1.75}
          aria-hidden
          className={row.status === 'running' ? 'spin' : undefined}
        />
      </span>
      <span className="rid" aria-hidden="true">
        R{row.round}
        {row.incumbent ? (
          <span className="inc" data-tip={INCUMBENT} data-side="right">
            <Trophy size={14} strokeWidth={1.75} aria-hidden />
          </span>
        ) : null}
      </span>
      {live === null ? (
        <span
          className={row.incumbent ? 'val mono is-inc' : 'val mono'}
          data-tip={row.valueTip ?? undefined}
          data-side="right"
          aria-hidden="true"
        >
          {row.value}
        </span>
      ) : (
        <Elapsed
          className="val"
          tip="Round elapsed"
          live={row.status === 'running'}
          ms={now => roundAgentElapsedMs(live, now)}
          aria-hidden
        />
      )}
      {provenance === 'official' ? (
        <span className="off" data-tip={OFFICIAL} data-side="right">
          <BadgeCheck size={14} strokeWidth={1.75} aria-hidden />
        </span>
      ) : (
        <span />
      )}
    </button>
  );
}
