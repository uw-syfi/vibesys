/** Registry reads behind the run-transport switch.

Budgets and correlation stay in the generated registry. This module only
selects the checkout and projects the protobuf into the helper types the
features already speak. The helper JSON channel is the off switch, a temporary
adapter owned by PR4.
*/
import type {ComputeSettings, MachineDescription} from '@vibesys/contracts/host-helper';
import type {HostId} from '@vibesys/hosts';
import {
  acceptDescribeMachine,
  acceptListCompute,
  callDescribeMachine,
  callListCompute,
  DescribeMachineResponseSchema,
  decodeHostRead,
  encodeHostRead,
  ListComputeResponseSchema,
  type OperationSignal,
  operationPolicies,
  projectMessage,
} from '@vibesys/proto';
import {newRequestId} from '@vibesys/request-identity';

export interface RegistryCatalog {
  describeMachine(
    host: HostId,
    expectedCheckout: string | null,
    signal: AbortSignal,
  ): Promise<MachineDescription>;
  listCompute(
    host: HostId,
    expectedCheckout: string | null,
    signal: AbortSignal,
  ): Promise<ComputeSettings>;
}

export interface RegistryReadPorts {
  checkout(host: HostId): Promise<string | null>;
  unary(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
}

/** The saved checkout, or a refusal when it is missing or no longer the one the caller named. */
export function requireCheckout(saved: string | null, expected: string | null): string {
  if (saved === null || saved.length === 0) throw new Error('Set a checkout first');
  if (expected !== null && expected !== saved) throw new Error('checkout changed');
  return saved;
}

export function registryTransport(
  checkout: string,
  send: (bytes: Uint8Array, signal: AbortSignal) => Promise<Uint8Array>,
): {
  invoke(
    id: number,
    payload: Uint8Array,
    totalMs: number,
    signal: OperationSignal,
  ): Promise<Uint8Array>;
} {
  return {
    async invoke(id, payload, _totalMs, signal) {
      if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
      if (!(signal instanceof AbortSignal)) throw new Error('operation cancelled');
      const policy = Object.values(operationPolicies).find(item => item.id === id);
      if (policy === undefined) throw new Error('unknown read');
      return decodeHostRead(
        policy.name,
        await send(encodeHostRead(checkout, policy.name, payload), signal),
      );
    },
  };
}

/** Catalog reads for one host. The gateway applies `readBudgets`; this does not keep another table. */
export function registryReads(ports: RegistryReadPorts): RegistryCatalog {
  async function transport(host: HostId, expected: string | null, signal: AbortSignal) {
    const checkout = requireCheckout(await ports.checkout(host), expected);
    if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
    return registryTransport(checkout, (bytes, owned) => ports.unary(host, bytes, owned));
  }
  return {
    async describeMachine(host, expectedCheckout, signal) {
      const response = await callDescribeMachine(
        await transport(host, expectedCheckout, signal),
        acceptDescribeMachine({requestId: newRequestId()}),
        signal,
      );
      const projected = projectMessage(DescribeMachineResponseSchema, response) as {
        machine: MachineDescription;
      };
      return projected.machine;
    },
    async listCompute(host, expectedCheckout, signal) {
      const response = await callListCompute(
        await transport(host, expectedCheckout, signal),
        acceptListCompute({requestId: newRequestId()}),
        signal,
      );
      const projected = projectMessage(ListComputeResponseSchema, response) as {
        settings: ComputeSettings;
      };
      return projected.settings;
    },
  };
}
