/**
 * A pane the frame draws around a view: the frame's kicker, title, facts and actions, with what the
 * view adds to them (`PaneHeader`, from the view's `ViewHeader`).
 */
import {Pane, type PaneResize, type PaneWidth, Row} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {PaneRole, PaneSizes} from '../../state/index.js';
import type {PaneHeader} from './views/props.js';

/**
 * How a pane of `role` is resized from the screen's `sizes`: the width given to the role, and a
 * new one goes back to it. Without sizes the pane keeps its default width and has no separator.
 */
export function paneResize(
  sizes: PaneSizes | undefined,
  role: PaneRole,
  label: string,
  edge: 'start' | 'end' = 'end',
): PaneResize | undefined {
  if (sizes === undefined) return undefined;
  return {
    width: sizes.widths[role] ?? null,
    changeWidth: width => sizes.change(role, width),
    label,
    edge,
  };
}

function joined(first: ReactNode | undefined, second: ReactNode | undefined): ReactNode {
  if (first === undefined) return second;
  if (second === undefined) return first;
  return (
    <Row gap="space-2" align="center">
      {first}
      {second}
    </Row>
  );
}

export function FramedPane(props: {
  readonly kicker: string;
  readonly title: string;
  readonly width: PaneWidth;
  readonly meta?: ReactNode;
  readonly actions?: ReactNode;
  readonly enterKey?: string;
  /** The body does not scroll: its view scrolls its own list. */
  readonly fixed?: boolean;
  /** The title is a name (an idea's). */
  readonly mono?: boolean;
  readonly resize?: PaneResize | undefined;
  readonly header: PaneHeader;
  readonly children: ReactNode;
}): JSX.Element {
  const {header} = props;
  const meta = joined(props.meta, header.meta);
  const actions = joined(header.actions, props.actions);
  return (
    <Pane
      kicker={header.kicker ?? props.kicker}
      title={header.title ?? props.title}
      width={props.width}
      {...(meta === undefined ? {} : {meta})}
      {...(actions === undefined ? {} : {actions})}
      {...(props.enterKey === undefined ? {} : {enterKey: props.enterKey})}
      {...(props.fixed === true ? {fixed: true} : {})}
      {...(props.mono === true ? {mono: true} : {})}
      {...(props.resize === undefined ? {} : {resize: props.resize})}
    >
      {props.children}
    </Pane>
  );
}
