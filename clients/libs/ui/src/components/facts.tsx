import type {JSX, ReactNode} from 'react';
import {Text} from './text.js';

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
  /** Facts that are whole sentences of their own, apart by the width of a group gap and with no mark between. */
  readonly spaced?: boolean;
  readonly children: ReactNode;
}): JSX.Element {
  const fit = props.fit ?? 'content';
  return (
    <div
      className="vs-factline"
      data-fit={fit}
      data-wrap={(props.wrap ?? fit === 'content') || undefined}
      data-spaced={props.spaced === true || undefined}
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

/**
 * A fact that is a figure and the words it counts ("12 measured"): the figure in the text color,
 * the words muted. They are one piece of text, so the space between them is a real space (a fact is
 * a flex box, which drops a space between its children and leaves a 2 px gap). `lead` words come
 * before the figure ("job places not read on 2 machines"). `hint` is what the pointer shows.
 */
export function CountedFact(props: {
  readonly figure: string;
  readonly words: string;
  readonly lead?: string;
  readonly hint?: string;
}): JSX.Element {
  return (
    <Fact {...(props.hint === undefined ? {} : {title: props.hint})}>
      <Text variant="small" tone="muted">
        {props.lead === undefined ? null : `${props.lead} `}
        <Text variant="small">{props.figure}</Text>
        {` ${props.words}`}
      </Text>
    </Fact>
  );
}

/** A fact in the quiet voice, with what it means on hover. */
export function MutedFact(props: {readonly text: string; readonly hint?: string}): JSX.Element {
  return (
    <Fact {...(props.hint === undefined ? {} : {title: props.hint})}>
      <Text variant="small" tone="muted">
        {props.text}
      </Text>
    </Fact>
  );
}
