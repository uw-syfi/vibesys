import {SidebarItem} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunRow, Screen, ShellActions, SidebarAction, SidebarState} from '../../state/index.js';

function sameScreen(left: Screen, right: Screen): boolean {
  if (left.kind !== 'run' || right.kind !== 'run') return left.kind === right.kind;
  return left.run.host === right.run.host && left.run.runId === right.run.runId;
}

/** One run as a row: the sidebar's Recent and Live lists and the All runs screen draw it alike. */
export function RunItem(props: {
  readonly row: RunRow;
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly actions: ShellActions;
}): JSX.Element {
  const {row, state, dispatch, actions} = props;
  const screen: Screen = {kind: 'run', run: row.run};
  return (
    <SidebarItem
      label={row.title}
      detail={row.trail === null ? row.detail : `${row.detail} · ${row.trail}`}
      hint={row.tooltip}
      {...(row.problem?.retry === true
        ? {
            action: {
              label: 'Retry',
              press: () => dispatch({type: 'retry-digest', run: row.run}),
            },
          }
        : {})}
      status={{tone: row.look.tone, label: row.look.word}}
      active={sameScreen(state.selection, screen)}
      press={() => dispatch({type: 'select', screen})}
      menu={[
        {id: 'window', label: 'Open in new window', press: () => actions.openWindow(row.run)},
        {id: 'archive', label: 'Archive', press: () => actions.archive(row.run)},
      ]}
    />
  );
}
