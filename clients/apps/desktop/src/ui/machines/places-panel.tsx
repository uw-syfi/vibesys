import type {EvalPlace, ListEvalPlacesResponse} from '@vibesys/orchestrator-proto';
import {Button, Card, Chip, Reserve, Row, Spacer, Table, type TableColumn, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {type Reach, type Remote, reachOf, unreadableDetail} from '../../state/index.js';
import {offerWords} from '../orchestrator/new-run/words.js';

/** One machine and what it reported of its eval places. */
export interface PlacesHost {
  readonly key: string;
  readonly label: string;
  readonly places: Remote<ListEvalPlacesResponse>;
}

const REACH_TONE: Readonly<Record<Reach, 'ok' | 'err' | 'neutral'>> = {
  reachable: 'ok',
  unreachable: 'err',
  'not probed': 'neutral',
};

const COLUMNS: readonly TableColumn<EvalPlace>[] = [
  {key: 'name', header: 'Name', lead: true, mono: true, cell: place => place.name},
  {
    key: 'reach',
    header: 'Reach',
    cell: place => {
      const reach = reachOf(place);
      const hint = unreadableDetail(place);
      return <Chip label={reach} tone={REACH_TONE[reach]} even {...(hint === '' ? {} : {hint})} />;
    },
  },
  {
    key: 'offers',
    header: 'Offers',
    cell: place => place.offers.map(offerWords).join(', '),
  },
];

const NOT_READ = <Chip label="Not read" tone="warn" />;

/**
 * A machine's eval places, read only: name, reach and offers, with a Refresh that has the host probe
 * them again. The status chip keeps its room while it is not shown, a read under way dims what is
 * there until the answer replaces it once, and the places never change from here.
 */
function MachinePlaces(props: {
  readonly host: PlacesHost;
  readonly refresh: (key: string) => void;
}): JSX.Element {
  const {host} = props;
  const {data, reading, failure} = host.places;
  return (
    <Card>
      <Row gap="space-3">
        <Text variant="section">{host.label}</Text>
        {failure === null ? (
          <Reserve>{NOT_READ}</Reserve>
        ) : (
          <Chip label="Not read" tone="warn" hint={failure} />
        )}
        <Spacer />
        <Button
          variant="quiet"
          size="sm"
          icon="refresh"
          label={`Refresh ${host.label}`}
          hint="Refresh"
          press={() => props.refresh(host.key)}
          {...(reading ? {disabledReason: 'Reading', quietReason: true} : {})}
        />
      </Row>
      <Table
        label={`${host.label} places`}
        columns={COLUMNS}
        rows={data?.places ?? []}
        rowKey={place => place.name}
        dim={reading}
      />
    </Card>
  );
}

/** The eval places of every machine, one card each, in the order of the machines. */
export function PlacesPanel(props: {
  readonly hosts: readonly PlacesHost[];
  readonly refresh: (key: string) => void;
}): JSX.Element {
  return (
    <>
      {props.hosts.map(host => (
        <MachinePlaces key={host.key} host={host} refresh={props.refresh} />
      ))}
    </>
  );
}
