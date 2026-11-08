import type {JSX, ReactNode} from 'react';

/**
 * What is on screen while it is read again: the data of the answer before, dimmed in place and out of
 * reach until the new answer swaps it, so a read moves nothing. `stale` is whether a new answer is
 * awaited; the box is the same one either way.
 */
export function Stale(props: {readonly stale: boolean; readonly children: ReactNode}): JSX.Element {
  return (
    <div className="vs-stale" data-stale={props.stale || undefined} aria-busy={props.stale}>
      {props.children}
    </div>
  );
}
