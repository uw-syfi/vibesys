/** A fake `SubscriptionPort`: the test plays main, sending updates and ending the port. */
export {type GpuSample, machineEntry, type Readings, runEntryOf, runsOf} from './entries.js';
export type {FarSide, PortRig} from './port-rig.js';

import type {HostKey} from '@vibesys/hosts';
import type {SubscribeRequest} from '@vibesys/proto';
import type {SubscriptionPort, SubscriptionUpdate} from '../index.js';

export interface FakePortCall {
  readonly host: HostKey;
  readonly request: SubscribeRequest;
  readonly key: string | undefined;
  /** Whether the window still holds the port. */
  readonly open: boolean;
  send(update: SubscriptionUpdate): void;
  end(error?: string): void;
}

function hears(key: string | undefined, update: SubscriptionUpdate): boolean {
  if (key === undefined) return true;
  if (update.kind === 'entry') return update.entry.key === key;
  if (update.kind === 'removed') return update.key === key;
  return true;
}

export class FakeSubscriptionPort implements SubscriptionPort {
  readonly calls: FakePortCall[] = [];
  /** The hosts the window asked to retry, in order. */
  readonly retries: HostKey[] = [];

  observe: SubscriptionPort['observe'] = (host, request, key, receive, ended) => {
    let open = true;
    const call: FakePortCall = {
      host,
      request,
      key,
      get open() {
        return open;
      },
      send: update => {
        // A keyed observation hears only its key's entries and removals, as main sends them.
        if (open && hears(key, update)) receive(update);
      },
      end: error => {
        if (!open) return;
        open = false;
        ended(error);
      },
    };
    this.calls.push(call);
    return {
      cancel: () => {
        open = false;
      },
    };
  };

  retry: SubscriptionPort['retry'] = host => {
    this.retries.push(host);
  };

  /** The calls the window still holds. */
  get held(): readonly FakePortCall[] {
    return this.calls.filter(call => call.open);
  }
}
