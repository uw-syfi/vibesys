import {FakeSubscriptionPort} from './testing/index.js';
import {subscriptionPortContract} from './testing/port-contract.js';
import type {FarSide} from './testing/port-rig.js';

/** The Fake as a rig: the test is main, and speaks for a host by sending to its held calls. */
subscriptionPortContract('FakeSubscriptionPort', () => {
  const port = new FakeSubscriptionPort();
  const far: FarSide = {
    live: (host, entry) => {
      for (const call of port.held.filter(each => each.host === host)) {
        call.send({kind: 'begin'});
        call.send({kind: 'entry', entry});
        call.send({kind: 'synced'});
        call.send({kind: 'control', control: {kind: 'live', since: 0}});
      }
    },
    holds: host => port.held.some(call => call.host === host),
    waits: () => Promise.resolve(),
    retried: host => port.retries.filter(each => each === host).length,
    remove: host => {
      for (const call of port.held.filter(each => each.host === host)) {
        call.send({kind: 'control', control: {kind: 'failed', reason: 'closed', retrying: false}});
        call.end();
      }
      return Promise.resolve();
    },
    drain: () => Promise.resolve(),
    later: () => Promise.resolve(),
  };
  return {port, far};
});
