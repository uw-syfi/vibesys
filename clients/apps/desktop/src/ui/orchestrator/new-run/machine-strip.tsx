import type {EvalPlace} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  Row,
  SegmentedControl,
  Stack,
  Stale,
  StatusDot,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {offeredPlaces, readable} from '../../../state/index.js';
import type {StartHost} from '../start-run-form.js';
import {offerWords, placeDetail, placeName} from './words.js';

/** The most accelerators a tab names before it says how many more there are. */
const HARDWARE_SHOWN = 3;

/**
 * A tab's second part: what the machine can evaluate on, such as `CPU · 8× H100`, which is what
 * decides where a run can go. Nothing before the places are read, and `no places` when it has none.
 */
const hardwareWord = (host: StartHost): string => {
  const places = host.evalPlaces.data?.places;
  if (places === undefined) return 'reading hardware';
  const offered = offeredPlaces(places);
  if (offered.length === 0) return 'no places to evaluate';
  const hardware = [
    ...new Set(
      offered.flatMap(place =>
        place.offers.length === 0 ? ['CPU'] : place.offers.map(offerWords),
      ),
    ),
  ];
  const more = hardware.length - HARDWARE_SHOWN;
  return `${hardware.slice(0, HARDWARE_SHOWN).join(' · ')}${more > 0 ? ` +${more}` : ''}`;
};

/**
 * The machine the page is about, first on every step: a prominent choice per machine and the places
 * a run's evaluations go to. While another machine is read
 * the strip keeps showing the one before, dimmed (`stale`), and swaps once, so the page never moves.
 */
export function MachineStrip(props: {
  readonly hosts: readonly StartHost[];
  /** The machine the person picked. */
  readonly chosen: string;
  /** The places of the machine on screen, which is the one before the pick while that is read. */
  readonly places: readonly EvalPlace[];
  readonly stale: boolean;
  /** The machine on screen could not be read. */
  readonly failure: string | null;
  readonly pick: (host: string) => void;
  readonly editMachines: () => void;
}): JSX.Element {
  const noMachines = props.hosts.length === 0;
  return (
    <Card>
      <Stack gap="space-3">
        <Row justify="between" gap="space-4" wrap>
          <Stack gap="space-1">
            <Text variant="heading">Choose a machine</Text>
            <Text tone="muted">Repositories and agents run here.</Text>
          </Stack>
          <Row gap="space-2">
            <StatusDot
              tone={
                props.failure !== null ? 'err' : noMachines ? 'warn' : props.stale ? 'live' : 'ok'
              }
              label={
                props.failure ??
                (noMachines
                  ? 'No machine connected'
                  : props.stale
                    ? 'Reading machine'
                    : 'Machine ready')
              }
            />
            <Button
              text="Add machine or server"
              icon="plus"
              variant="secondary"
              size="sm"
              press={props.editMachines}
            />
          </Row>
        </Row>
        <Row gap="space-4" wrap>
          <Text variant="section">Machine</Text>
          {noMachines ? (
            <Text tone="muted">No machine connected. You can still browse every example.</Text>
          ) : (
            <SegmentedControl
              label="Machine"
              value={props.chosen}
              options={props.hosts.map(host => ({
                value: host.key,
                label: `${host.label} · ${hardwareWord(host)}`,
              }))}
              change={props.pick}
            />
          )}
        </Row>
        <Row gap="space-3" wrap>
          <Text variant="section">Evals on</Text>
          <Stale stale={props.stale}>
            <Row gap="space-2" wrap>
              {offeredPlaces(props.places).map(place => (
                <Chip key={place.name} label={`${placeName(place)} · ${placeDetail(place)}`} />
              ))}
              {!props.stale && props.failure === null && !props.places.some(readable) ? (
                <Button
                  text="Set up an eval place"
                  variant="quiet"
                  size="sm"
                  press={props.editMachines}
                />
              ) : null}
            </Row>
          </Stale>
        </Row>
      </Stack>
    </Card>
  );
}
