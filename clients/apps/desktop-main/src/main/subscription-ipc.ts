/**
 * The window's way to a subscription. The generic gateway port refuses Subscribe, so a window asks
 * here, and the hub in main shares one stream among every window and feature that watches the
 * same topic of the same host. The port is the observation: it ends when the window goes away, and
 * the hub stops the stream when the last observation does.
 */
import {encodeUpdate, gatewayReply, parseSubscribeCall, SUBSCRIPTION_CHANNEL} from '@vibesys/ipc';
import type {SubscriptionPort} from '@vibesys/subscription';
import {registerAbort} from '@vibesys/transport';
import {type PortCall, type PortIpc, servePort} from './port-ipc.js';
import type {WelcomeInvokeEvent} from './welcome-ipc.js';

/** What one window's request does: observe until the hub or the window ends it. */
export function subscriptionCall(hub: SubscriptionPort): PortCall {
  return (value, signal, send) => {
    const call = parseSubscribeCall(value);
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
