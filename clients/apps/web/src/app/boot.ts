/**
 * What the browser page starts as, decided from its launch URL and what this tab remembered: the
 * replay demo (with the reason a gateway was refused, if one was), a live session, or a live
 * session that first exchanges its launch capability for a browser-session credential.
 */
import {
  browserSessionForGateway,
  type GatewayBootstrapTarget,
  type GatewayTarget,
  resolveGatewayTarget,
} from '../entities/gateway.js';
import type {Remembered} from '../platform/gateway-storage.js';

export type BootPlan =
  | {readonly kind: 'demo'; readonly error: string | null}
  | {
      readonly kind: 'live';
      readonly target: GatewayTarget;
      /** The remembered credential for this gateway; null when the cookie must serve alone. */
      readonly browserSession: string | null;
    }
  /** Exchange `target.bootstrapUrl` for a credential, then go live (or to the demo on failure). */
  | {readonly kind: 'bootstrap'; readonly target: GatewayBootstrapTarget};

export function bootPlan(
  launchHref: string,
  remembered: Remembered,
  historyState: unknown = null,
): BootPlan {
  let target: GatewayTarget | null;
  try {
    target = resolveGatewayTarget(launchHref, remembered.gateway, historyState);
  } catch (reason) {
    return {kind: 'demo', error: reason instanceof Error ? reason.message : String(reason)};
  }
  if (target === null) return {kind: 'demo', error: null};
  const {bootstrapUrl} = target;
  if (bootstrapUrl !== null) return {kind: 'bootstrap', target: {...target, bootstrapUrl}};
  return {
    kind: 'live',
    target,
    browserSession: browserSessionForGateway(remembered.browserSession, target.gatewayUrl),
  };
}
