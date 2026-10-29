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
  cardAction,
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
      case 'none':
        return null;
      case 'unsupported':
      case 'failed':
        return <Text tone="muted">Unavailable</Text>;
    }
  }
  const columns: readonly TableColumn<PlaceRow>[] = [
    {key: 'name', header: 'Name', lead: true, cell: row => row.name},
    {key: 'reach', header: 'Reached', mono: true, wide: true, cell: row => row.reach},
    {key: 'hardware', header: 'Hardware', wide: true, cell: row => row.hardware},
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
      control: true,
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

/** The one press that mends a connection: Reconnect when unreachable, Sign in when waiting for it. */
function Fix(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, machine, dispatch} = props;
  const action = cardAction(machine);
  if (action === null) return null;
  const busy = machine.checking !== null || state.pending?.host === machine.host;
  const words =
    action === 'sign-in'
      ? {idle: 'Sign in', busy: 'Signing in…'}
      : {idle: 'Reconnect', busy: 'Reconnecting…'};
  return (
    <Button
      size="sm"
      text={busy ? words.busy : words.idle}
      {...(busy ? {disabledReason: CHECKING} : {})}
      press={() =>
        dispatch({type: action === 'sign-in' ? 'sign-in' : 'reconnect', host: machine.host})
      }
    />
  );
}

function MachineCard(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
  readonly runs: number;
  readonly incomplete: boolean;
  readonly showRuns: (host: HostKey) => void;
  readonly openSettings: (host: HostKey) => void;
}): JSX.Element {
  const {state, machine, dispatch, runs} = props;
  const status = cardStatus(machine);
  const hardware = machineHardware(machine);
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
          <Fix state={state} machine={machine} dispatch={dispatch} />
          <Button size="sm" text="Settings" press={() => props.openSettings(machine.host)} />
        </Row>
        {hardware === null && runs === 0 && !props.incomplete ? null : (
          <Row gap="space-5" wrap>
            {hardware === null ? null : <Text>{hardware}</Text>}
            <Busy machine={machine} />
            {runs === 0 && !props.incomplete ? null : (
              <Button
                variant="quiet"
                size="sm"
                text={`${runs === 1 ? '1 run' : `${runs} runs`}${props.incomplete ? ' (incomplete)' : ''}`}
                press={() => props.showRuns(machine.host)}
              />
            )}
          </Row>
        )}
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
  readonly incompleteHosts?: ReadonlySet<HostKey>;
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
            incomplete={props.incompleteHosts?.has(machine.host) ?? false}
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
