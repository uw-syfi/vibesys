/**
 * The Machines page: one card per machine (its state, hardware, how busy it is, the runs on it,
 * Reconnect and Settings), and under each the clusters and clouds reached from it as nested rows,
 * each with Test. Add machine is last. Labels, numbers and status words only; the nesting says
 * which machine a cluster is reached from. Render is a function of the store's state; presses
 * dispatch its actions, except Show runs and Settings, which ask the shell to change screens.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  Banner,
  Button,
  Card,
  Chip,
  EmptyState,
  Meter,
  Page,
  Row,
  Stack,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  cardStatus,
  type Machine,
  type MachinesAction,
  type MachinesState,
  machineHardware,
  machineLoad,
  type PlaceRow,
  placeRows,
  placesReading,
} from '../../state/index.js';
import {AddMachine} from './machines-screen.js';

type Dispatch = (action: MachinesAction) => void;

const CHECKING = 'Checking';

function Busy(props: {readonly machine: Machine}): JSX.Element | null {
  const load = machineLoad(props.machine);
  if (load === null) return null;
  return (
    <Row gap="space-2">
      <Meter
        inline
        label={`${load.live} live of ${load.slots}`}
        value={load.share}
        tone={load.share >= 1 ? 'accent' : 'ok'}
      />
      <Text>{load.live} live</Text>
    </Row>
  );
}

function Places(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, machine, dispatch} = props;
  const rows = placeRows(state, machine);
  const reading = placesReading(state, machine);
  if (rows.length === 0) {
    switch (reading) {
      case 'loading':
        return <Text tone="muted">Reading</Text>;
      case 'ready':
        return <Text tone="muted">None</Text>;
      case 'unsupported':
      case 'failed':
        return <Text tone="muted">Unavailable</Text>;
      case 'none':
        return null;
    }
  }
  const columns: readonly TableColumn<PlaceRow>[] = [
    {key: 'name', header: 'Name', lead: true, cell: row => row.name},
    {key: 'reach', header: 'Reached', mono: true, wide: true, cell: row => row.reach},
    {key: 'hardware', header: 'Hardware', mono: true, wide: true, cell: row => row.hardware},
    {
      key: 'checked',
      header: 'Checked',
      mono: true,
      cell: row => (
        <Text tone={row.checked.tone === 'neutral' ? 'muted' : row.checked.tone}>
          {row.testing ? 'Testing' : row.checked.word}
        </Text>
      ),
    },
    {
      key: 'test',
      header: 'Test',
      align: 'end',
      cell: row =>
        row.testable ? (
          <Button
            variant="quiet"
            size="sm"
            text={row.testing ? 'Testing…' : 'Test'}
            {...(row.testing ? {disabledReason: 'Testing'} : {})}
            press={() => dispatch({type: 'test-cluster', host: machine.host, name: row.name})}
          />
        ) : null,
    },
  ];
  return (
    <Table
      label={`Clusters and clouds on ${machine.label}`}
      columns={columns}
      rows={rows}
      rowKey={row => row.key}
    />
  );
}

function MachineCard(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
  readonly runs: number;
  readonly showRuns: (host: HostKey) => void;
  readonly openSettings: (host: HostKey) => void;
}): JSX.Element {
  const {state, machine, dispatch, runs} = props;
  const status = cardStatus(machine);
  const busy = machine.checking !== null || state.pending?.host === machine.host;
  return (
    <Card>
      <Stack gap="space-4">
        <Row gap="space-3" wrap>
          <Stack grow>
            <Text as="h2" variant="heading">
              {machine.label}
            </Text>
          </Stack>
          <Chip dot label={status.word} tone={status.tone} />
          <Button
            size="sm"
            text={busy ? 'Reconnecting…' : 'Reconnect'}
            {...(busy ? {disabledReason: CHECKING} : {})}
            press={() => dispatch({type: 'reconnect', host: machine.host})}
          />
          <Button size="sm" text="Settings" press={() => props.openSettings(machine.host)} />
        </Row>
        <Row gap="space-5" wrap>
          <Text>{machineHardware(machine)}</Text>
          <Busy machine={machine} />
          <Row gap="space-2">
            <Text>{runs === 1 ? '1 run' : `${runs} runs`}</Text>
            <Button
              variant="quiet"
              size="sm"
              text="Show runs"
              press={() => props.showRuns(machine.host)}
            />
          </Row>
        </Row>
        <Places state={state} machine={machine} dispatch={dispatch} />
      </Stack>
    </Card>
  );
}

/**
 * The Machines page over its store's `state`. `runCounts` is the runs on each machine (from the
 * run digests); `showRuns` and `openSettings` ask the shell to change screens for a machine.
 */
export function MachinesPage(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
  readonly runCounts: ReadonlyMap<HostKey, number>;
  readonly showRuns: (host: HostKey) => void;
  readonly openSettings: (host: HostKey) => void;
}): JSX.Element {
  const {state, dispatch} = props;
  const header = (
    <Text as="h1" variant="title">
      Machines
    </Text>
  );
  return (
    <Page header={header}>
      <Stack>
        {state.listProblem === null ? null : (
          <Banner tone="err" title="The machines could not be read" body={state.listProblem} />
        )}
        {state.actionProblem === null ? null : <Banner tone="err" title={state.actionProblem} />}
        {state.listing !== null && state.machines.length === 0 ? (
          <Card>
            <EmptyState title="Reading" />
          </Card>
        ) : null}
        {state.machines.map(machine => (
          <MachineCard
            key={machine.host}
            state={state}
            machine={machine}
            dispatch={dispatch}
            runs={props.runCounts.get(machine.host) ?? 0}
            showRuns={props.showRuns}
            openSettings={props.openSettings}
          />
        ))}
        <AddMachine state={state} dispatch={dispatch} />
        {state.adding === null ? (
          <Row>
            <Button text="Add machine" press={() => dispatch({type: 'open-add'})} />
          </Row>
        ) : null}
      </Stack>
    </Page>
  );
}
