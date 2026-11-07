import type {JSX} from 'react';

/** How many characters of the end a name keeps when its middle is cut. */
const TAIL = 8;

/**
 * A name cut in its middle when there is no room: the start gives way (with an ellipsis) and the end
 * stays, so names that share a prefix (`node-b-a-vega`, `node-b-a-vesta`) still tell apart. The whole
 * name is the text on hover. It takes the width its parent leaves it, so the parent must be able to
 * shrink (`min-width: 0`).
 */
export function MiddleText(props: {readonly text: string}): JSX.Element {
  const cut = Math.min(TAIL, Math.floor(props.text.length / 2));
  const at = props.text.length - cut;
  return (
    <span className="vs-middle" title={props.text}>
      <span className="vs-middle-head">{props.text.slice(0, at)}</span>
      <span className="vs-middle-tail">{props.text.slice(at)}</span>
    </span>
  );
}
