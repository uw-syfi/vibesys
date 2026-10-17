/**
 * A pane the frame draws around a view: the frame's kicker, title, facts and actions, with what the
 * view adds to them (`PaneHeader`, from the view's `ViewHeader`).
 */
import {Pane, type PaneWidth, Row} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {PaneHeader} from './views/props.js';

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
    >
      {props.children}
    </Pane>
  );
}
