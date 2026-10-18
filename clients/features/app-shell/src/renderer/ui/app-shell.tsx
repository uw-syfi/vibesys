import {AppFrame, Button, Row, SidebarGroup, SidebarItem, SidebarSection, Text} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  type Clock,
  liveMachineGroups,
  type RunRow,
  recentGroups,
  type Screen,
  type ShellActions,
  type SidebarAction,
  type SidebarState,
} from '../../state/index.js';
import {RunItem} from './run-item.js';

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
 * Settings) beside the selected screen, which `screen` renders. The sidebar can be hidden, leaving
 * a narrow rail with New run and Settings; its toggle is in the window's title-bar row beside the
 * window controls, in the same place whether the sidebar is shown or hidden. `shortcut` is the toggle's keyboard shortcut, as a tooltip shows it.
 */
export function AppShell(props: {
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly actions: ShellActions;
  readonly version: string;
  readonly shortcut: string;
  /** The moment and zone Recent's times are worded in; the app passes the viewer's own. */
  readonly clock: () => Clock;
  readonly screen: (screen: Screen) => ReactNode;
}): JSX.Element {
  const {state, dispatch, actions} = props;
  const live = liveMachineGroups(state);
  const recent = recentGroups(state, props.clock());
  const recentCount = recent.groups.reduce((total, machine) => total + machine.rows.length, 0);
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
      control={toggle}
      rail={{
        label: 'Quick navigation',
        content: (
          <>
            <Button
              variant="primary"
              icon="plus"
              label="New run"
              press={select({kind: 'new-run'})}
            />
            <Button
              variant="quiet"
              icon="gear"
              label="Settings"
              press={select({kind: 'settings'})}
            />
          </>
        ),
      }}
      onCollapse={() => dispatch({type: 'toggle-sidebar'})}
      navPinned={
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
          </Row>
          <Button
            variant="primary"
            full
            icon="plus"
            text="New run"
            press={select({kind: 'new-run'})}
          />
        </>
      }
      nav={
        <>
          {live.length === 0 ? null : (
            <SidebarSection title="Live">
              {live.map(group => (
                <SidebarGroup key={group.title} title={group.title}>
                  {group.rows.map(row => item(row))}
                </SidebarGroup>
              ))}
            </SidebarSection>
          )}
          <SidebarSection
            title="Recent"
            showAll={{
              label: recent.more === 0 ? 'Show all' : `Show all (${recentCount + recent.more})`,
              press: select({kind: 'all-runs'}),
            }}
          >
            {recent.groups.length === 0 ? (
              <Text variant="small" tone="muted">
                Runs you open show up here.
              </Text>
            ) : (
              recent.groups.map(machine => (
                <SidebarGroup key={machine.host} title={machine.title}>
                  {machine.rows.map(row => item(row))}
                </SidebarGroup>
              ))
            )}
          </SidebarSection>
        </>
      }
      footer={
        <SidebarItem
          label="Settings"
          icon="gear"
          active={state.selection.kind === 'settings' || state.selection.kind === 'machines'}
          press={select({kind: 'settings'})}
        />
      }
    >
      {props.screen(state.selection)}
    </AppFrame>
  );
}
