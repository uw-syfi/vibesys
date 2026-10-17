import {AppFrame, Button, Row, SidebarItem, SidebarSection, Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  liveRows,
  machineCounts,
  type RunRow,
  recentRows,
  type Screen,
  type ShellActions,
  type SidebarAction,
  type SidebarState,
} from '../../state/index.js';

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

/**
 * The icon-only control that hides or shows the sidebar: named, and described with its shortcut,
 * for what pressing it does now.
 */
function SidebarToggle(props: {
  readonly shown: boolean;
  readonly shortcut: string;
  readonly press: () => void;
}): JSX.Element {
  const label = props.shown ? 'Hide sidebar' : 'Show sidebar';
  return (
    <Button
      variant="quiet"
      size="sm"
      icon="sidebar"
      label={label}
      hint={`${label} (${props.shortcut})`}
      press={props.press}
    />
  );
}

/**
 * The app's one window: the sidebar (New run, Live, Recent with "Show all", Machines and
 * Settings) beside the selected screen, which `screen` renders. The sidebar can be hidden
 * completely; its toggle is in the sidebar's header, and in the main area's top strip while the
 * sidebar is hidden. `shortcut` is the toggle's keyboard shortcut, as a tooltip shows it.
 */
export function AppShell(props: {
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly actions: ShellActions;
  readonly version: string;
  readonly shortcut: string;
  readonly screen: (screen: Screen) => ReactNode;
}): JSX.Element {
  const {state, dispatch, actions} = props;
  const live = liveRows(state);
  const recent = recentRows(state);
  const machines = machineCounts(state);
  const toggle = (
    <SidebarToggle
      shown={state.shown}
      shortcut={props.shortcut}
      press={() => dispatch({type: 'toggle-sidebar'})}
    />
  );
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
      navHidden={!state.shown}
      reveal={toggle}
      nav={
        <>
          <Row justify="between">
            <Row gap="space-3">
              <Text variant="section" tone="default">
                VibeSys
              </Text>
              <Text variant="code" tone="muted">
                {props.version}
              </Text>
            </Row>
            {toggle}
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
