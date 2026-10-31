/**
 * The Machines page: one card per machine (its state, hardware, how busy it is, the runs on it,
 * Reconnect and Settings), and under each the clusters and clouds reached from it as nested rows,
 * each with Test. Add machine is last. Labels, numbers and status words only; the nesting says
 * which machine a cluster is reached from. Render is a function of the store's state; presses
 * dispatch its actions, except Show runs and Settings, which ask the shell to change screens.
 */
import type {HostKey} from '@vibesys/hosts';
import {
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
  type HostLive,
  type Machine,
  type MachinesAction,
  type MachinesState,
  machineHardware,
  machineLoad,
  type PlaceRow,
  placeRows,
  placesReading,
} from '../../state/index.js';
import {MachineStats} from './machine-stats.js';
import {AddMachine} from './machines-screen.js';

type Dispatch = (action: MachinesAction) => void;

/** Draws what `render` makes of a host's live stats: the slot that reaches the effects. */
export type LiveSlot = (host: HostKey, render: (live: HostLive) => JSX.Element) => JSX.Element;

const CHECKING = 'Checking';

function Busy(props: {
  readonly machine: Machine;
  /** The runs live on the machine now, or null while its run list is not read. */
  readonly live: number | null;
}): JSX.Element | null {
  const load = machineLoad(props.machine, props.live);
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

/**
 * The press that asks a host for readings again: the machine's own fix (Reconnect, Sign in) is the
 * one press while its connection is the problem, and otherwise a host that cannot give readings
 * offers Retry.
 */
function retryOf(machine: Machine, live: HostLive | null): (() => void) | null {
  if (live === null || cardAction(machine) !== null) return null;
  return live.view.kind === 'failed' && live.view.retry ? live.retry : null;
}

function CardHeader(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
  readonly live: HostLive | null;
  readonly openSettings: (host: HostKey) => void;
}): JSX.Element {
  const {state, machine, dispatch} = props;
  const status = cardStatus(machine, props.live?.view ?? null);
  const retry = retryOf(machine, props.live);
  return (
    <Row gap="space-3" wrap>
      <Stack grow>
        <Text as="h2" variant="heading">
          {machine.label}
        </Text>
      </Stack>
      <Chip dot label={status.word} tone={status.tone} />
      <Fix state={state} machine={machine} dispatch={dispatch} />
      {retry === null ? null : (
        <Button size="sm" text="Retry" label={`Retry ${machine.label}`} press={retry} />
      )}
      <Button size="sm" text="Settings" press={() => props.openSettings(machine.host)} />
    </Row>
  );
}

function CardFacts(props: {
  readonly machine: Machine;
  readonly liveRuns: number | null;
  readonly runs: number;
  readonly incomplete: boolean;
  readonly showRuns: (host: HostKey) => void;
}): JSX.Element | null {
  const {machine, runs} = props;
  const hardware = machineHardware(machine);
  if (hardware === null && runs === 0 && !props.incomplete) return null;
  return (
    <Row gap="space-5" wrap>
      {hardware === null ? null : <Text>{hardware}</Text>}
      <Busy machine={machine} live={props.liveRuns} />
      {runs === 0 && !props.incomplete ? null : (
        <Button
          variant="quiet"
          size="sm"
          text={`${runs === 1 ? '1 run' : `${runs} runs`}${props.incomplete ? ' (incomplete)' : ''}`}
          press={() => props.showRuns(machine.host)}
        />
      )}
    </Row>
  );
}

function MachineCard(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
  readonly live: LiveSlot | undefined;
  readonly liveRuns: number | null;
  readonly runs: number;
  readonly incomplete: boolean;
  readonly showRuns: (host: HostKey) => void;
  readonly openSettings: (host: HostKey) => void;
}): JSX.Element {
  const {state, machine, dispatch} = props;
  const card = (live: HostLive | null): JSX.Element => (
    <Card>
      <Stack gap="space-4">
        <CardHeader
          state={state}
          machine={machine}
          dispatch={dispatch}
          live={live}
          openSettings={props.openSettings}
        />
        <CardFacts
          machine={machine}
          liveRuns={props.liveRuns}
          runs={props.runs}
          incomplete={props.incomplete}
          showRuns={props.showRuns}
        />
        {live === null ? null : <MachineStats live={live} />}
        <Places state={state} machine={machine} dispatch={dispatch} />
      </Stack>
    </Card>
  );
  return props.live === undefined ? card(null) : props.live(machine.host, card);
}

/**
 * The Machines page over its store's `state`. `runCounts` is the runs on each machine (from the
 * run digests); `showRuns` and `openSettings` ask the shell to change screens for a machine.
 */
export function MachinesPage(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
  readonly runCounts: ReadonlyMap<HostKey, number>;
  /** The runs live on each machine whose run list is read. */
  readonly liveRuns: ReadonlyMap<HostKey, number>;
  /** A machine's live stats, handed to its card to draw; without it the cards show none. */
  readonly live?: LiveSlot;
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
            live={props.live}
            liveRuns={props.liveRuns.get(machine.host) ?? null}
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
