import {
  AppFrame,
  Button,
  Row,
  SidebarItem,
  SidebarSection,
  type StatusTone,
  Text,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  liveRows,
  machineCounts,
  type RunRow,
  type RunRowStatus,
  recentRows,
  type Screen,
  type ShellActions,
  type SidebarAction,
  type SidebarState,
  statusWord,
} from '../../state/index.js';

const TONES: Readonly<Record<RunRowStatus, StatusTone>> = {
  live: 'live',
  paused: 'warn',
  finished: 'ok',
  failed: 'err',
  stopped: 'neutral',
  unknown: 'neutral',
};

function sameScreen(left: Screen, right: Screen): boolean {
  if (left.kind !== 'run' || right.kind !== 'run') return left.kind === right.kind;
  return left.run.host === right.run.host && left.run.runId === right.run.runId;
}

function RunItem(props: {
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
      detail={row.detail}
      status={{tone: TONES[row.status], label: statusWord(row.status)}}
      active={sameScreen(state.selection, screen)}
      press={() => dispatch({type: 'select', screen})}
      menu={[
        {id: 'window', label: 'Open in new window', press: () => actions.openWindow(row.run)},
        {id: 'archive', label: 'Archive', press: () => actions.archive(row.run)},
      ]}
    />
  );
}

/**
 * The app's one window: the sidebar (New run, Live, Recent with "Show all", Machines and
 * Settings) beside the selected screen, which `screen` renders.
 */
export function AppShell(props: {
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly actions: ShellActions;
  readonly version: string;
  readonly screen: (screen: Screen) => ReactNode;
}): JSX.Element {
  const {state, dispatch, actions} = props;
  const live = liveRows(state);
  const recent = recentRows(state);
  const machines = machineCounts(state);
  const select = (screen: Screen) => () => dispatch({type: 'select', screen});
  const item = (row: RunRow) => (
    <RunItem
      key={`${row.run.host} ${row.run.runId}`}
      row={row}
      state={state}
      dispatch={dispatch}
      actions={actions}
    />
  );
  return (
    <AppFrame
      label="VibeSys"
      nav={
        <>
          <Row justify="between">
            <Text variant="section" tone="default">
              VibeSys
            </Text>
            <Text variant="code" tone="muted">
              {props.version}
            </Text>
          </Row>
          <Button
            variant="primary"
            full
            icon="plus"
            text="New run"
            press={select({kind: 'new-run'})}
          />
          {live.length === 0 ? null : (
            <SidebarSection title="Live">{live.map(item)}</SidebarSection>
          )}
          <SidebarSection
            title="Recent"
            showAll={{
              label:
                recent.more === 0 ? 'Show all' : `Show all (${recent.rows.length + recent.more})`,
              press: select({kind: 'all-runs'}),
            }}
          >
            {recent.rows.length === 0 ? (
              <Text variant="small" tone="muted">
                Runs you open show up here.
              </Text>
            ) : (
              recent.rows.map(item)
            )}
          </SidebarSection>
        </>
      }
      footer={
        <>
          <SidebarItem
            label="Machines"
            icon="monitor"
            count={machines.count}
            {...(machines.attention === 0
              ? {}
              : {
                  detail: `${machines.attention} need${machines.attention === 1 ? 's' : ''} attention`,
                })}
            active={state.selection.kind === 'machines'}
            press={select({kind: 'machines'})}
          />
          <SidebarItem
            label="Settings"
            icon="gear"
            active={state.selection.kind === 'settings'}
            press={select({kind: 'settings'})}
          />
        </>
      }
    >
      {props.screen(state.selection)}
    </AppFrame>
  );
}
