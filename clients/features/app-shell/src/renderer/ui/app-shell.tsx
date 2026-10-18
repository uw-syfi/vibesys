import {
  AppFrame,
  Banner,
  Button,
  Row,
  SidebarGroup,
  SidebarItem,
  SidebarSection,
  Text,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  type Clock,
  liveMachineGroups,
  type RunRow,
  recentGroups,
  recentNotice,
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

/** An unfinished New run, as the sidebar lists it: what it is of so far, and where it stands. */
export interface SidebarDraft {
  readonly title: string;
  readonly detail: string;
  /** Open New run where it was left. */
  readonly open: () => void;
  readonly discard: () => void;
}

/** What the narrow rail keeps when the sidebar is hidden: New run and Settings. */
function QuickRail(props: {readonly select: (screen: Screen) => () => void}): JSX.Element {
  return (
    <>
      <Button
        variant="primary"
        icon="plus"
        label="New run"
        press={props.select({kind: 'new-run'})}
      />
      <Button
        variant="quiet"
        icon="gear"
        label="Settings"
        press={props.select({kind: 'settings'})}
      />
    </>
  );
}

/** The unfinished New run, listed above the runs. */
function DraftSection(props: {
  readonly draft: SidebarDraft;
  readonly active: boolean;
}): JSX.Element {
  const {draft} = props;
  return (
    <SidebarSection title="Draft">
      <SidebarItem
        label={draft.title}
        detail={draft.detail}
        icon="plus"
        active={props.active}
        press={draft.open}
        menu={[{id: 'discard', label: 'Discard draft', press: draft.discard}]}
      />
    </SidebarSection>
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
  /** The unfinished New run of this window, listed above the runs; none when there is not one. */
  readonly draft?: SidebarDraft | null;
  readonly screen: (screen: Screen) => ReactNode;
}): JSX.Element {
  const {state, dispatch, actions} = props;
  const live = liveMachineGroups(state);
  const recent = recentGroups(state, props.clock());
  const skipped = recentNotice(state.skippedRecent);
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
      rail={{label: 'Quick navigation', content: <QuickRail select={select} />}}
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
          {props.draft == null ? null : (
            <DraftSection draft={props.draft} active={state.selection.kind === 'new-run'} />
          )}
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
            {skipped === null ? null : (
              <Banner tone="warn" title={skipped.title} body={skipped.body} />
            )}
            {recent.groups.length === 0 && skipped === null ? (
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
