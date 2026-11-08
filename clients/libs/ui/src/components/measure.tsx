import type {JSX, ReactNode} from 'react';

/** A column of fields no wider than a form reads comfortably, whatever the page's width. */
export function Measure(props: {readonly children: ReactNode}): JSX.Element {
  return <div className="vs-measure">{props.children}</div>;
}
