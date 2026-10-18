import {Button, Card, EmptyState, Page, Row, SidebarGroup, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {AllRunsGroup, ShellActions, SidebarAction, SidebarState} from '../../state/index.js';
import {RunItem} from './run-item.js';

const MACHINE_NOTES = {
  loading: 'Listing runs…',
  failed: 'Some runs could not be listed.',
} as const;

function Machine(props: {
  readonly group: AllRunsGroup;
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly actions: ShellActions;
}): JSX.Element {
  const {group, state, dispatch, actions} = props;
  const note = group.state === 'listed' ? null : MACHINE_NOTES[group.state];
  return (
    <SidebarGroup title={group.title} note={`${group.rows.length}`}>
      {group.rows.map(row => (
        <RunItem
          key={`${row.run.host} ${row.run.runId}`}
          row={row}
          state={state}
          dispatch={dispatch}
          actions={actions}
        />
      ))}
      {note !== null ? (
        <Text variant="small" tone="muted">
          {note}
        </Text>
      ) : group.rows.length === 0 ? (
        <Text variant="small" tone="muted">
          No runs yet.
        </Text>
      ) : null}
    </SidebarGroup>
  );
}

/**
 * Every run of every project on each connected machine, grouped by machine and newest first, each
 * row the sidebar's own: pressing one opens the run. "Reload" lists every machine again.
 */
export function AllRunsScreen(props: {
  readonly groups: readonly AllRunsGroup[];
  readonly state: SidebarState;
  readonly dispatch: (action: SidebarAction) => void;
  readonly actions: ShellActions;
}): JSX.Element {
  const {groups, state, dispatch, actions} = props;
  return (
    <Page>
      <Card>
        <Stack gap="space-4">
          <Row justify="between">
            <Text as="h1" variant="title">
              All runs
            </Text>
            <Button
              variant="secondary"
              size="sm"
              text="Reload"
              press={() => dispatch({type: 'reload-all-runs'})}
            />
          </Row>
          {groups.length === 0 ? (
            <EmptyState
              title="No machine is connected"
              body="Add a machine in Settings to list its runs here."
            />
          ) : (
            groups.map(group => (
              <Machine
                key={group.host}
                group={group}
                state={state}
                dispatch={dispatch}
                actions={actions}
              />
            ))
          )}
        </Stack>
      </Card>
    </Page>
  );
}
