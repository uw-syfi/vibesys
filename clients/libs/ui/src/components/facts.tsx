import type {JSX, ReactNode} from 'react';

/**
 * Facts about one thing that never overlap or break inside: each `Fact` keeps its whole text on
 * one line, except one that `shrink`s, which gives way first and ends in an ellipsis. The facts
 * flow onto another line when they do not fit, unless `wrap` is false: then the line is always one
 * line high and a fact that does not fit is cut. With `fit` `parent` the line takes the width of
 * what holds it and adds nothing to it (a caption under a figure must not widen the figure's
 * block, so it also never wraps); by default it is as wide as its facts, up to its container.
 */
export function FactLine(props: {
  readonly fit?: 'content' | 'parent';
  readonly wrap?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const fit = props.fit ?? 'content';
  return (
    <div
      className="vs-factline"
      data-fit={fit}
      data-wrap={(props.wrap ?? fit === 'content') || undefined}
    >
      {props.children}
    </div>
  );
}

/**
 * One fact of a `FactLine`. A `shrink` fact (a name that may be long) is cut with an ellipsis when
 * the line is narrow; `title` shows its whole text on hover.
 */
export function Fact(props: {
  readonly shrink?: boolean;
  readonly title?: string;
  readonly children: ReactNode;
}): JSX.Element {
  return (
    <span className="vs-fact" data-shrink={props.shrink === true || undefined} title={props.title}>
      {props.children}
    </span>
  );
}
