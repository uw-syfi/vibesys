/**
 * A machine's live stats on its card: gauges for CPU, memory and the GPUs, dimmed with the age of
 * the value when the host stopped delivering. The row is one line high whatever it holds, so the
 * card keeps its height as the host goes live, stale, away and back. The card's status chip says
 * which; this says what the host last read. Labels, numbers and ages only.
 */
import {Gauge, Readings, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {HostLive} from '../../state/index.js';

export function MachineStats(props: {readonly live: HostLive}): JSX.Element {
  const {view, age} = props.live;
  if (view.kind !== 'values') return <Readings />;
  const {stale} = view;
  const note = stale === null ? undefined : `stale${age === null ? '' : ` ${age}`}`;
  return (
    <Readings>
      {view.gauges.map(gauge => (
        <Gauge
          key={gauge.label}
          label={gauge.label}
          share={gauge.share}
          figure={gauge.figure}
          figureWidth={gauge.width}
          tone={gauge.tone}
          dim={stale !== null}
          {...(note === undefined ? {} : {note})}
        />
      ))}
      {stale === null || age === null ? null : (
        <Text variant="small" tone={stale.reconnecting ? 'muted' : 'warn'}>
          {`${age} ago`}
        </Text>
      )}
    </Readings>
  );
}
