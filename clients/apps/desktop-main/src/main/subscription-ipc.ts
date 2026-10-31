/**
 * The window's way to a subscription. The generic gateway port refuses Subscribe, so a window asks
 * here, and the hub in main shares one stream among every window and feature that watches the
 * same topic of the same host. The port is the observation: it ends when the window goes away, and
 * the hub stops the stream when the last observation does.
 */
import {
  encodeUpdate,
  gatewayReply,
  parseSubscriptionCall,
  SUBSCRIPTION_CHANNEL,
} from '@vibesys/ipc';
import type {SubscriptionPort} from '@vibesys/subscription';
import {registerAbort} from '@vibesys/transport';
import {type PortCall, type PortIpc, servePort} from './port-ipc.js';
import type {WelcomeInvokeEvent} from './welcome-ipc.js';

/**
 * What one window's request does: observe until the hub or the window ends it, or ask the hub to
 * retry a host, which is over as soon as it is asked.
 */
export function subscriptionCall(hub: SubscriptionPort): PortCall {
  return (value, signal, send) => {
    const call = parseSubscriptionCall(value);
    if (call.kind === 'retry') {
      hub.retry(call.host);
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      const observation = hub.observe(
        call.host,
        call.request,
        call.key,
        update => send(gatewayReply(encodeUpdate(update))),
        error => (error === undefined ? resolve() : reject(new Error(error))),
      );
      const release = (): void => {
        observation.cancel();
        resolve();
      };
      const registration = registerAbort(signal, release);
      if (registration.aborted) release();
    });
  };
}

export function installSubscriptionIpc(
  ipc: PortIpc,
  {hub, admits}: {hub: SubscriptionPort; admits: (event: WelcomeInvokeEvent) => boolean},
): void {
  servePort(ipc, SUBSCRIPTION_CHANNEL, admits, subscriptionCall(hub));
}
