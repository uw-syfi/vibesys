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
import {placeDetail, placeName} from './words.js';

/** A tab's second word: how many places the machine evaluates on, or nothing before it is read. */
const placesWord = (host: StartHost): string => {
  const places = host.evalPlaces.data?.places;
  return places === undefined ? 'eval places' : `${offeredPlaces(places).length} eval places`;
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
                label: `${host.label} · ${placesWord(host)}`,
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
