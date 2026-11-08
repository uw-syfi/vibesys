import type {JSX, ReactNode} from 'react';

/**
 * Items in one line that never wraps: what does not fit is cut at the end, under a fade, so the
 * line is the same height whatever it holds and nothing below it moves.
 */
export function ClipRow(props: {readonly children: ReactNode}): JSX.Element {
  return <div className="vs-clip-row">{props.children}</div>;
}
