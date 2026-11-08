import type {EvalPlace} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  ClipRow,
  Row,
  SegmentedControl,
  Spacer,
  Stack,
  Stale,
  StatusDot,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {type MachineFacts, offeredPlaces} from '../../../state/index.js';
import type {StartHost} from '../start-run-form.js';
import {placeDetail, placeName} from './words.js';

/** A figure of the strip, or a dash for what the host has not said yet. */
const figure = (count: number | null): string => (count === null ? '—' : String(count));

/** A tab's second word: how many places the machine evaluates on, or nothing before it is read. */
const placesWord = (host: StartHost): string => {
  const places = host.evalPlaces.data?.places;
  return places === undefined ? 'eval places' : `${offeredPlaces(places).length} eval places`;
};

/** The counts line: what fits the machine, and what is in its Repositories folder. */
const countsWords = (facts: MachineFacts): string =>
  `${figure(facts.fitting)} catalog tasks fit · ${figure(facts.repositories)} repositories · ${figure(facts.withTasks)} with tasks`;

/**
 * The machine the page is about, first on every step: a tab per machine with its eval places, a line
 * of what that machine has, and the places a run's evaluations go to. While another machine is read
 * the strip keeps showing the one before, dimmed (`stale`), and swaps once, so the page never moves.
 */
export function MachineStrip(props: {
  readonly hosts: readonly StartHost[];
  /** The machine the person picked. */
  readonly chosen: string;
  /** The places and counts of the machine on screen, which is the one before the pick while that is read. */
  readonly places: readonly EvalPlace[];
  readonly facts: MachineFacts;
  readonly stale: boolean;
  /** The machine on screen could not be read. */
  readonly failure: string | null;
  readonly pick: (host: string) => void;
  readonly editMachines: () => void;
}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-3">
        <Row gap="space-4" wrap>
          <ClipRow fixed>
            <Text variant="section">Machine</Text>
          </ClipRow>
          <SegmentedControl
            label="Machine"
            value={props.chosen}
            options={props.hosts.map(host => ({
              value: host.key,
              label: `${host.label} · ${placesWord(host)}`,
            }))}
            change={props.pick}
          />
          <Spacer />
          <Row gap="space-2">
            <StatusDot
              tone={props.failure !== null ? 'err' : props.stale ? 'live' : 'ok'}
              label={props.failure ?? (props.stale ? 'Reading' : 'Read')}
            />
            <Stale stale={props.stale}>
              <Text tone="muted">{countsWords(props.facts)}</Text>
            </Stale>
          </Row>
        </Row>
        <Row gap="space-3">
          <ClipRow fixed>
            <Text variant="section">Evals on</Text>
          </ClipRow>
          <Stale stale={props.stale}>
            <ClipRow>
              {offeredPlaces(props.places).map(place => (
                <Chip key={place.name} label={`${placeName(place)} · ${placeDetail(place)}`} />
              ))}
            </ClipRow>
          </Stale>
          <Spacer />
          <Button text="Edit in Machines" variant="quiet" size="sm" press={props.editMachines} />
        </Row>
      </Stack>
    </Card>
  );
}
