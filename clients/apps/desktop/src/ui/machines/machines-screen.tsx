import type {ListEvalPlacesResponse} from '@vibesys/orchestrator-proto';
import {Page, Stack} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type MachineRow,
  type MachineView,
  machineEntries,
  machinesFacts,
  machineTableRows,
  type PlaceAsk,
  placesAsked,
  type Remote,
  settleAsks,
} from '../../state/index.js';
import type {MachineControls} from './add-machine/machine-controls.js';
import {MachinesFactsLine} from './machines-facts.js';
import {MachinesTable} from './machines-table.js';

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
 * Machines: one table, a row for each machine the app is connected to (its one status and its live
 * readings) and under it the places jobs can run at, with the page's summary line under its title. With
 * `controls` the page adds and removes machines; without them it lists.
 */
export function MachinesScreen(props: {
  readonly rows: readonly MachineRow[];
  /** The Add machine and remove controls; a window without them lists the machines only. */
  readonly controls?: MachineControls;
  /** Each machine's live CPU, memory and GPU readings; without them the rows have no readings. */
  readonly stats?: readonly MachineStatsHost[];
  /** Each machine's eval places and the refresh of one; without them the rows have no places. */
  readonly places?: {
    readonly hosts: readonly PlacesHost[];
    readonly refresh: (key: string) => void;
  };
}): JSX.Element {
  const {controls, places} = props;
  const entries = machineEntries(props.rows, {
    ...(props.stats === undefined ? {} : {stats: props.stats}),
    ...(places === undefined ? {} : {places: places.hosts}),
  });
  // What the person asked to read again: kept here, and settled as the hosts' answers come in.
  const [asks, setAsks] = useState<ReadonlyMap<string, PlaceAsk>>(new Map());
  const settled = settleAsks(asks, entries);
  if (settled !== asks) setAsks(settled);
  return (
    <Page
      width="fill"
      state="ready"
      title="Machines"
      {...(controls === undefined ? {} : {actions: controls.action})}
      meta={<MachinesFactsLine facts={machinesFacts(entries)} />}
    >
      <Stack gap="space-4">
        {controls?.form}
        <MachinesTable
          rows={machineTableRows(entries, settled)}
          readings={props.stats !== undefined}
          places={places !== undefined}
          actions={{
            ...(places === undefined
              ? {}
              : {
                  refresh: key => {
                    setAsks(placesAsked(settled, key));
                    places.refresh(key);
                  },
                }),
            ...(controls === undefined ? {} : {remove: controls.remove}),
          }}
          selected={controls?.selected ?? null}
          select={controls?.select}
        />
      </Stack>
      {controls?.toasts}
    </Page>
  );
}
