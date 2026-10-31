/**
 * A host's live stats, composed: the subscription's effects handed to the card that draws them.
 * The page takes it as a slot, because presentational code reaches no effects.
 */
import type {HostKey} from '@vibesys/hosts';
import type {JSX} from 'react';
import type {HostLive} from '../state/index.js';
import {type LiveStats, useHostStats} from './effects/live.js';

/** What `render` draws for `host`, given the host's live stats. */
export function WithHostLive(props: {
  readonly live: LiveStats;
  readonly host: HostKey;
  readonly render: (live: HostLive) => JSX.Element;
}): JSX.Element {
  return props.render(useHostStats(props.live, props.host));
}
