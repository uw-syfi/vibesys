import type {ListEvalPlacesResponse} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  EmptyState,
  type Figure,
  FigureStrip,
  Grid,
  Page,
  Row,
  Stack,
  Stale,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type FleetOverview,
  hostFromKey,
  type MachineEntry,
  type MachineRow,
  type MachineTableRow,
  type MachineView,
  machineEntries,
  machinesFacts,
  machineTableRows,
  type PlaceAsk,
  placesAsked,
  type Reach,
  type Remote,
  settleAsks,
} from '../../state/index.js';
import {HostStatusChip} from '../host-status-chip.js';
import {dollars} from '../orchestrator/fleet-words.js';
import {offerWords} from '../orchestrator/new-run/words.js';
import type {MachineControls} from './add-machine/machine-controls.js';
import {Reading} from './machine-cells.js';
import {MachinesFactsLine} from './machines-facts.js';

/** One machine's live CPU, memory and GPU readings, read only. */
export interface MachineStatsHost {
  readonly key: string;
  readonly label: string;
  readonly view: MachineView;
}

/** One machine and what it reported of its eval places. */
export interface PlacesHost {
  readonly key: string;
  readonly label: string;
  readonly places: Remote<ListEvalPlacesResponse>;
}

/**
 * How the app reaches a machine, in one quiet line: the way in, where it is, and the user when the
 * saved key names one. A fact SSH resolves by itself (a user, a port) is not listed as a stand-in.
 */
function connectionLine(row: MachineRow): string {
  const host = hostFromKey(row.key);
  if (host.kind === 'local') return 'This computer';
  const at = host.alias.lastIndexOf('@');
  const user = at < 0 ? null : host.alias.slice(0, at);
  const destination = at < 0 ? host.alias : host.alias.slice(at + 1);
  return user === null ? `SSH to ${destination}` : `SSH to ${destination} as ${user}`;
}

/** What the machine is carrying now: a figure nobody reported is a muted dash, never a zero. */
function loadFigures(load: FleetOverview | undefined): readonly Figure[] {
  if (load === undefined) return [];
  const dash = {value: '\u2014', tone: 'muted' as const};
  return [
    {
      label: 'Live runs',
      value: String(load.live),
      detail: load.ended === 0 ? 'none ended' : `${load.ended} ended`,
      hint: 'Runs on this machine that are starting, running or stopping',
    },
    {
      label: 'Attention',
      value: String(load.attention),
      detail: load.attention === 0 ? 'none waiting' : `of ${load.live} live`,
      hint: 'Live runs that want the operator: a stall, stuck jobs, a limit or kept checkouts',
      tone: load.attention > 0 ? 'warn' : 'muted',
    },
    {
      label: 'Agents',
      ...(load.agentsWorking === null ? dash : {value: String(load.agentsWorking)}),
      hint: 'Agents starting, in a turn or waiting on a subagent, across the live runs',
    },
    {
      label: 'Spend',
      ...(load.liveCostUsd === null ? dash : {value: dollars(load.liveCostUsd)}),
      detail: 'live runs',
      hint: 'Priced turns of the runs still working',
    },
  ];
}

function MachineReadings(props: {readonly view: MachineView}): JSX.Element {
  if (props.view._tag !== 'Shown') return <Reading view={props.view} which="cpu" />;
  return (
    <Grid min="tile" gap="space-3">
      {(['cpu', 'memory', 'gpu'] as const).map(which => (
        <Stack key={which} gap="space-1">
          <Text variant="small" tone="muted">
            {which === 'cpu' ? 'CPU' : which === 'gpu' ? 'GPU' : 'Memory'}
          </Text>
          <Reading view={props.view} which={which} />
        </Stack>
      ))}
    </Grid>
  );
}

type PlaceRow = Exclude<MachineTableRow, {readonly kind: 'machine'}>;

const REACH_TONE: Readonly<Record<Reach, 'neutral' | 'err'>> = {
  reachable: 'neutral',
  unreachable: 'err',
  'not probed': 'neutral',
};

const REACH_WORD: Readonly<Record<Reach, string>> = {
  reachable: 'Reachable',
  unreachable: 'Unreachable',
  'not probed': 'Not probed',
};

const NOTE_WORD: Readonly<Record<Extract<PlaceRow, {readonly kind: 'note'}>['note'], string>> = {
  waiting: 'Waiting for first probe',
  none: 'No eval places',
  'not-read': 'Last probe failed',
};

function PlaceName(props: {readonly row: PlaceRow}): JSX.Element {
  const {row} = props;
  if (row.kind === 'note') {
    return (
      <Text variant="small" tone="muted">
        {NOTE_WORD[row.note]}
      </Text>
    );
  }
  return (
    <Stale stale={row.dim}>
      <Text variant="code">{row.place.name}</Text>
    </Stale>
  );
}

function PlaceReach(props: {readonly row: PlaceRow}): JSX.Element | null {
  const {row} = props;
  if (row.kind === 'note') {
    return row.note === 'not-read' ? (
      <Chip
        label="Probe failed"
        tone="warn"
        {...(row.failure === null ? {} : {hint: row.failure})}
      />
    ) : null;
  }
  return (
    <Stale stale={row.dim}>
      <Chip
        label={REACH_WORD[row.reach]}
        tone={REACH_TONE[row.reach]}
        {...(row.detail === '' ? {} : {hint: row.detail})}
      />
    </Stale>
  );
}

function PlaceOffers(props: {readonly row: PlaceRow}): JSX.Element | null {
  const {row} = props;
  if (row.kind === 'note') return null;
  const offers = row.reach === 'reachable' ? row.place.offers.map(offerWords).join(', ') : '';
  return (
    <Stale stale={row.dim}>
      <Text variant="small" tone="muted">
        {offers === '' && row.reach === 'reachable' ? 'CPU only' : offers}
      </Text>
    </Stale>
  );
}

const PLACE_COLUMNS: readonly TableColumn<PlaceRow>[] = [
  {key: 'name', header: 'Name', lead: true, cell: row => <PlaceName row={row} />},
  {key: 'reach', header: 'Reach', fit: true, cell: row => <PlaceReach row={row} />},
  {key: 'offers', header: 'Offers', cell: row => <PlaceOffers row={row} />},
];

function MachinePlaces(props: {
  readonly entry: MachineEntry;
  readonly ask: PlaceAsk;
  readonly refresh: (key: string) => void;
}): JSX.Element | null {
  const {entry} = props;
  if (entry.places === null) return null;
  const rows = machineTableRows([entry], new Map([[entry.row.key, props.ask]])).filter(
    (row): row is PlaceRow => row.kind !== 'machine',
  );
  return (
    <Stack gap="space-2">
      <Row gap="space-3" align="center">
        <Text variant="section">Eval places</Text>
        <Button
          variant="quiet"
          size="sm"
          icon="refresh"
          label={`Refresh job places on ${entry.row.label}`}
          hint={`Refresh job places on ${entry.row.label}`}
          press={() => props.refresh(entry.row.key)}
          {...(entry.places.reading ? {disabledReason: 'Reading', quietReason: true} : {})}
        />
      </Row>
      <Table
        label={`${entry.row.label} places`}
        columns={PLACE_COLUMNS}
        rows={rows}
        rowKey={row => row.key}
      />
    </Stack>
  );
}

function MachineCard(props: {
  readonly entry: MachineEntry;
  readonly repositoriesDirectory: string;
  readonly load: FleetOverview | undefined;
  readonly ask: PlaceAsk;
  readonly refreshPlaces: ((key: string) => void) | undefined;
  readonly remove: ((key: string) => void) | undefined;
  readonly selected: boolean;
}): JSX.Element {
  const {entry} = props;
  const {row} = entry;
  return (
    <Card
      section={row.label}
      beside={props.selected ? <Chip label="Added" tone="ok" /> : undefined}
      action={
        <Row gap="space-2">
          <HostStatusChip status={row.status} />
          {row.key === 'local' || props.remove === undefined ? null : (
            <Button
              variant="quiet"
              size="sm"
              icon="cross"
              label={`Remove ${row.label} from this app`}
              hint={`Remove ${row.label} from this app`}
              press={() => props.remove?.(row.key)}
            />
          )}
        </Row>
      }
    >
      <Stack gap="space-5">
        <Stack gap="space-1">
          <Text variant="small" tone="muted">
            {connectionLine(row)}
          </Text>
          {props.repositoriesDirectory === '' ? null : (
            <Text variant="small" tone="muted">
              {`Repositories in `}
              <Text variant="code">{props.repositoriesDirectory}</Text>
            </Text>
          )}
        </Stack>
        {props.load === undefined ? null : (
          <FigureStrip label={`Load on ${row.label}`} figures={loadFigures(props.load)} />
        )}
        {entry.stats === null ? null : (
          <Stack gap="space-2">
            <Text variant="section">Live resources</Text>
            <MachineReadings view={entry.stats} />
          </Stack>
        )}
        {props.refreshPlaces === undefined ? null : (
          <MachinePlaces entry={entry} ask={props.ask} refresh={props.refreshPlaces} />
        )}
      </Stack>
    </Card>
  );
}

/**
 * Machines: one responsive operator card per connected host. Each card joins the host's canonical
 * connection state, saved repository folder, live readings and eval places by stable host key.
 */
export function MachinesScreen(props: {
  readonly rows: readonly MachineRow[];
  /** The Add machine and remove controls; a window without them lists the machines only. */
  readonly controls?: MachineControls;
  /** Opens the dedicated Add machine page. */
  readonly addMachine?: () => void;
  /** Each machine's live CPU, memory and GPU readings; without them the cards have no readings. */
  readonly stats?: readonly MachineStatsHost[];
  /** Each machine's eval places and the refresh of one; without them the cards have no places. */
  readonly places?: {
    readonly hosts: readonly PlacesHost[];
    readonly refresh: (key: string) => void;
  };
  /** The explicit repositories folder of each machine; absent means the host chooses its default. */
  readonly repositories?: Readonly<Record<string, string>>;
  /** What each machine's runs are doing now, by host key; without it the cards say nothing of load. */
  readonly load?: Readonly<Record<string, FleetOverview>>;
}): JSX.Element {
  const {controls, places} = props;
  const entries = machineEntries(props.rows, {
    ...(props.stats === undefined ? {} : {stats: props.stats}),
    ...(places === undefined ? {} : {places: places.hosts}),
  });
  const [asks, setAsks] = useState<ReadonlyMap<string, PlaceAsk>>(new Map());
  const settled = settleAsks(asks, entries);
  if (settled !== asks) setAsks(settled);
  const refresh =
    places === undefined
      ? undefined
      : (key: string): void => {
          setAsks(placesAsked(settled, key));
          places.refresh(key);
        };
  return (
    <Page
      width="wide"
      state="ready"
      title="Machines"
      meta={<MachinesFactsLine facts={machinesFacts(entries)} />}
      actions={
        props.addMachine === undefined ? null : (
          <Button
            variant="primary"
            icon="plus"
            text="Add machine or server"
            press={props.addMachine}
          />
        )
      }
    >
      {entries.length === 0 ? (
        <EmptyState
          title="No machines connected"
          body="Add an SSH machine or server. This machine appears automatically when local runs are available."
        />
      ) : (
        <Grid min="main">
          {entries.map(entry => (
            <MachineCard
              key={entry.row.key}
              entry={entry}
              repositoriesDirectory={props.repositories?.[entry.row.key] ?? ''}
              load={props.load?.[entry.row.key]}
              ask={settled.get(entry.row.key) ?? 'none'}
              refreshPlaces={refresh}
              remove={controls?.remove}
              selected={controls?.selected === entry.row.key}
            />
          ))}
        </Grid>
      )}
      {controls?.toasts}
    </Page>
  );
}
