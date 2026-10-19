import {Scrubber} from '@vibesys/ui';
import type {JSX} from 'react';
import {type RunView, scrubberView, type TraceLog, viewAtPosition} from '../../state/index.js';

/**
 * The time scrubber: replays the run's trace at any earlier span event, live or ended, with ticks
 * where each planning call started. Nothing is shown before the page has an event to scrub to.
 */
export function RunScrubber(props: {
  readonly log: TraceLog;
  readonly view: RunView;
  readonly ended: boolean;
  /** The host's wall time now (see `runWallNow`); null when the run has no live clock. */
  readonly wall: number | null;
  readonly change: (view: RunView) => void;
}): JSX.Element | null {
  const {log, view, ended, change} = props;
  const shown = scrubberView(log, view, ended, props.wall);
  if (shown === null) return null;
  return (
    <Scrubber
      label="Run position"
      value={shown.value}
      last={shown.last}
      live={shown.latest}
      pastLabel="Replay"
      latestLabel={shown.latestLabel}
      backLabel={shown.backLabel}
      position={shown.position}
      ticks={shown.ticks}
      tickWord="Call"
      change={index => change(viewAtPosition(log, index))}
      backToLive={() => change({at: 'live'})}
    />
  );
}
