import {Card} from '@vibesys/ui';
import type {JSX} from 'react';
import type {MachineView} from '../../state/index.js';
import {MachineReadings} from './machine-readings.js';

/** One machine's live CPU, memory and GPU readings, read only. */
export interface MachineStatsHost {
  readonly key: string;
  readonly label: string;
  readonly view: MachineView;
}

/** Every machine's live readings, one card each, in the order of the machines. */
export function MachineStatsPanel(props: {
  readonly hosts: readonly MachineStatsHost[];
}): JSX.Element {
  return (
    <>
      {props.hosts.map(host => (
        <Card key={host.key} section={host.label}>
          <MachineReadings view={host.view} />
        </Card>
      ))}
    </>
  );
}
