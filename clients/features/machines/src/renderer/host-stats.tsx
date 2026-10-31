/**
 * A host's live stats, composed: the subscription's effects over the presentational card row. The
 * page takes it as a slot, because presentational code reaches no effects.
 */
import type {HostKey} from '@vibesys/hosts';
import type {JSX} from 'react';
import {type LiveStats, useHostStats} from './effects/live.js';
import {MachineStats} from './ui/machine-stats.js';

export function HostStats(props: {readonly live: LiveStats; readonly host: HostKey}): JSX.Element {
  const {view, age, retry} = useHostStats(props.live, props.host);
  return <MachineStats view={view} age={age} retry={retry} />;
}
