/**
 * The machines feature's welcome requests: the saved machines, what one machine's facts say now,
 * what a path on a machine holds, and removing a machine. A machine is a host plus the VibeSys
 * checkout the app runs there, saved in `hosts.json`; adding one or changing its checkout is the
 * hosts feature's checked `setCheckout`, and sign-in, path completion and cancelling a checkout
 * change are the hosts feature's requests too.
 */
import {
  type CheckoutInspectReply,
  type HostId,
  type HostKey,
  hostFromKey,
  type MachineDescription,
  validHostPath,
} from '@vibesys/hosts';
import type {WelcomeResult} from './types.js';
import type {ChannelOf} from './values.js';

export const MACHINES_CHANNELS = {
  machines: 'vibesys:welcome:machines',
  machine: 'vibesys:welcome:machine',
  inspectCheckout: 'vibesys:welcome:inspect-checkout',
  removeMachine: 'vibesys:welcome:remove-machine',
} as const;

/** A saved machine: a host, its label, and the VibeSys checkout the app runs there. */
export interface MachineEntry {
  readonly host: HostKey;
  readonly label: string;
  readonly checkout: string;
}

/**
 * What a machine's facts say now, from which its status follows (no status is stored): `sign-in`
 * when its connection waits for credentials, `checkout-not-found` when no VibeSys checkout is at
 * its path, `unavailable` when it cannot be reached for another reason (offline, no `uv`, a
 * checkout of another protocol version), `needs-update` when its helper lacks a feature this app
 * needs (`REQUIRED_HELPER_FEATURES`), and otherwise `ready`; both carry what `machine.describe`
 * says, or null from a helper that predates it.
 */
export type MachineReport =
  | {readonly kind: 'ready'; readonly facts: MachineDescription | null}
  | {readonly kind: 'needs-update'; readonly facts: MachineDescription | null}
  | {readonly kind: 'sign-in'; readonly detail: string}
  | {readonly kind: 'checkout-not-found'; readonly detail: string}
  | {readonly kind: 'unavailable'; readonly detail: string};

/** What a path on a machine holds (`checkout.inspect`), or that its helper cannot say. */
export type CheckoutInspection =
  | CheckoutInspectReply['inspection']
  | {readonly state: 'unsupported'};

/** The welcome bridge's requests the machines feature makes. */
export interface MachinesWelcomeBridge {
  machines(): Promise<WelcomeResult<MachineEntry[]>>;
  machine(host: HostKey): Promise<WelcomeResult<MachineReport>>;
  inspectCheckout(host: HostKey, path: string): Promise<WelcomeResult<CheckoutInspection>>;
  /** Forget `host`: the app no longer runs VibeSys there. Answers with the machines left. */
  removeMachine(host: HostKey): Promise<WelcomeResult<MachineEntry[]>>;
}

export type MachinesRequest =
  | {readonly kind: 'machines'}
  | {readonly kind: 'machine'; readonly host: HostId}
  | {readonly kind: 'inspect-checkout'; readonly host: HostId; readonly path: string}
  | {readonly kind: 'remove-machine'; readonly host: HostId};

/** The typed request on `channel` with `args`; throws naming the first invalid value. */
export function parseMachinesRequest(
  channel: ChannelOf<typeof MACHINES_CHANNELS>,
  args: readonly unknown[],
): MachinesRequest {
  const [first, second] = args;
  switch (channel) {
    case MACHINES_CHANNELS.machines:
      return {kind: 'machines'};
    case MACHINES_CHANNELS.machine:
      return {kind: 'machine', host: hostFromKey(first)};
    case MACHINES_CHANNELS.inspectCheckout:
      return {
        kind: 'inspect-checkout',
        host: hostFromKey(first),
        path: validHostPath(second, 'the path to inspect'),
      };
    case MACHINES_CHANNELS.removeMachine:
      return {kind: 'remove-machine', host: hostFromKey(first)};
  }
}
