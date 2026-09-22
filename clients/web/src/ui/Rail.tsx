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
import type {RailModel, RailRow, RoundStatus} from '../model.js';
import {Elapsed} from './Elapsed.js';
import './Rail.css';

export interface RailProps {
  state: 'loading' | 'unattached' | 'ready';
  model: RailModel;
  selected: number | null;
  /** The experiments query failed; shown under the rows with Retry. */
  error: string | null;
  /** Phone only, until the first sheet opens. */
  hint: boolean;
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

export function Rail({state, model, selected, error, hint, onSelect, onRetry}: RailProps) {
  const list = useRef<HTMLOListElement>(null);
  // Keyboard selection moves focus with it; on the phone strip the selected chip scrolls into view.
  useEffect(() => {
    if (selected === null) return;
    const current = list.current?.querySelector<HTMLElement>('[aria-current="true"]');
    if (current === null || current === undefined) return;
    if (list.current?.contains(document.activeElement)) current.focus();
    current.scrollIntoView({block: 'nearest', inline: 'nearest'});
  }, [selected]);

  return (
    <nav className="rail" aria-label="Rounds">
      {state === 'loading' ? (
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
              <Row row={row} selected={row.round === selected} onSelect={onSelect} />
            </li>
          ))}
        </ol>
      )}
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

function Row({
  row,
  selected,
  onSelect,
}: {
  row: RailRow;
  selected: boolean;
  onSelect: (round: number) => void;
}) {
  const {word, Icon} = STATUS[row.status];
  const live = row.live;
  const provenance = row.value === null ? '' : row.official ? ', official' : ', provisional';
  return (
    <button
      type="button"
      className="rrow"
      aria-current={selected ? 'true' : undefined}
      onClick={() => onSelect(row.round)}
    >
      <span className={`ico st-${row.status}`} data-tip={word} data-side="right">
        <Icon
          size={16}
          strokeWidth={1.75}
          aria-hidden
          className={row.status === 'running' ? 'spin' : undefined}
        />
      </span>
      <span className="rid">
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
        >
          {row.value}
        </span>
      ) : (
        <Elapsed
          className="val"
          tip="Round elapsed"
          live={row.status === 'running'}
          ms={now => roundAgentElapsedMs(live, now)}
        />
      )}
      {row.official ? (
        <span className="off" data-tip={OFFICIAL} data-side="right">
          <BadgeCheck size={14} strokeWidth={1.75} aria-hidden />
        </span>
      ) : (
        <span />
      )}
      <span className="sr-only">
        {word.toLowerCase()}
        {row.incumbent ? ', incumbent' : ''}
        {provenance}
      </span>
    </button>
  );
}
