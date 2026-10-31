/**
 * The subscription hub as a `SubscriptionPort` rig, for the port's contract suite: the far side is
 * a host's gateway streams over fake time, so a retry, a removal and a release show as the hub's
 * own streams opening, ending and closing.
 */
import type {HostKey} from '@vibesys/hosts';
import type {PortRig} from '@vibesys/subscription/contract';
import {SUBSCRIPTION_LINGER_MS, type Subscriptions} from '../shell/subscriptions.js';
import {hubRig, settle} from './hub-rig.js';

export function hubPortRig(): PortRig & {readonly hub: Subscriptions} {
  const {clock, gateway, subscriptions} = hubRig();
  const asked = new Map<HostKey, number>();
  const streamsOf = (host: HostKey) => gateway.streams.filter(stream => stream.host === host);
  return {
    hub: subscriptions,
    port: subscriptions.port,
    far: {
      live: (host, entry) => {
        const stream = streamsOf(host)
          .filter(each => each.open)
          .at(-1);
        // A host with no stream open has no one to speak to.
        if (stream === undefined) return;
        stream.reached('runs');
        stream.send({case: 'entry', value: entry});
        stream.send({case: 'synced', value: {}});
      },
      holds: host => streamsOf(host).some(stream => stream.open),
      waits: async host => {
        streamsOf(host)
          .filter(each => each.open)
          .at(-1)
          ?.fail();
        await settle();
        asked.set(host, streamsOf(host).length);
      },
      retried: host => streamsOf(host).length - (asked.get(host) ?? 0),
      remove: async host => {
        subscriptions.closeHost(host);
        await settle();
      },
      drain: settle,
      later: async () => {
        clock.advanceBy(SUBSCRIPTION_LINGER_MS);
        await settle();
      },
    },
  };
}
