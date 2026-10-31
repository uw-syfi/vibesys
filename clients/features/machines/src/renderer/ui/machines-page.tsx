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
  type RunsView,
  type StatsView,
} from '../../state/index.js';
import {MachineStats} from './machine-stats.js';
import {AddMachine} from './machines-screen.js';

type Dispatch = (action: MachinesAction) => void;

/** Draws what `render` makes of a host's live stats: the slot that reaches the effects. */
export type LiveSlot = (host: HostKey, render: (live: HostLive) => JSX.Element) => JSX.Element;

const CHECKING = 'Checking';

function Busy(props: {
  readonly machine: Machine;
  /** The runs the machine holds, as it last said; null until it has said. */
  readonly runs: RunsView | null;
}): JSX.Element | null {
  const load = machineLoad(props.machine, props.runs?.alive ?? null);
  if (load === null) return null;
  // What a host last said stays when it stops delivering: dimmed, in the same place.
  const dim = props.runs?.stale === true;
  return (
    <Row gap="space-2">
      <Meter
        inline
        dim={dim}
        label={`${load.live} live of ${load.slots}`}
        value={load.share}
        tone={load.share >= 1 ? 'accent' : 'ok'}
      />
      <Text {...(dim ? {tone: 'muted' as const} : {})}>{load.live} live</Text>
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
  /** What the host's stream says, which a card that is delivering needs no Reconnect for. */
  readonly stream: StatsView | null;
}): JSX.Element | null {
  const {state, machine, dispatch} = props;
  const action = cardAction(machine, props.stream);
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
  if (live === null || cardAction(machine, live.view) !== null) return null;
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
      <Fix state={state} machine={machine} dispatch={dispatch} stream={props.live?.view ?? null} />
      {retry === null ? null : (
        <Button size="sm" text="Retry" label={`Retry ${machine.label}`} press={retry} />
      )}
      <Button size="sm" text="Settings" press={() => props.openSettings(machine.host)} />
    </Row>
  );
}

function CardFacts(props: {
  readonly machine: Machine;
  /** The runs the machine holds, from the one subscription; null until the host has said. */
  readonly runs: RunsView | null;
  readonly showRuns: (host: HostKey) => void;
}): JSX.Element | null {
  const {machine, runs} = props;
  const hardware = machineHardware(machine);
  if (hardware === null && runs === null) return null;
  const total = runs?.total ?? 0;
  return (
    <Row gap="space-5" wrap>
      {hardware === null ? null : <Text>{hardware}</Text>}
      <Busy machine={machine} runs={runs} />
      {runs === null || total === 0 ? null : (
        <Button
          variant="quiet"
          size="sm"
          text={total === 1 ? '1 run' : `${total} runs`}
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
        <CardFacts machine={machine} runs={live?.runs ?? null} showRuns={props.showRuns} />
        {live === null ? null : <MachineStats live={live} />}
        <Places state={state} machine={machine} dispatch={dispatch} />
      </Stack>
    </Card>
  );
  return props.live === undefined ? card(null) : props.live(machine.host, card);
}

/**
 * The Machines page over its store's `state`. The runs on each machine, live and in all, come from
 * its `runs` subscription through `live`, one source for both; `showRuns` and `openSettings` ask
 * the shell to change screens for a machine.
 */
export function MachinesPage(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
  /** A machine's live data, handed to its card to draw; without it the cards show none. */
  readonly live?: LiveSlot;
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
