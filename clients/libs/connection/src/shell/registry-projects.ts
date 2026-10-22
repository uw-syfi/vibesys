/** Registry project create, copy, and destination checks behind the run-transport switch.

Budgets stay in the generated registry. The helper JSON channel is the off switch.
*/
import type {CreatedProject, ExampleDestination} from '@vibesys/contracts/host-helper';
import type {HostId} from '@vibesys/hosts';
import {
  acceptCopyProject,
  acceptCreateExample,
  acceptInspectDestination,
  CopyProjectResponseSchema,
  CreatedProjectSchema,
  type CreateExampleResponse,
  callCopyProject,
  callCreateExample,
  callInspectDestination,
  decodeHostMutate,
  encodeHostMutate,
  InspectDestinationResponseSchema,
  type OperationSignal,
  operationPolicies,
  projectMessage,
} from '@vibesys/proto';
import {registryTransport, requireCheckout} from './registry-reads.js';

export interface RegistryProjectPorts {
  checkout(host: HostId): Promise<string | null>;
  read(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
  mutate(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
}

export interface RegistryProjects {
  destination(
    host: HostId,
    example: string,
    destination: string,
    signal: AbortSignal,
  ): Promise<{state: ExampleDestination; project: string | null}>;
  create(
    host: HostId,
    example: string,
    destination: string,
    signal: AbortSignal,
  ): Promise<{kind: 'created'; created: CreatedProject} | {kind: 'taken'}>;
  copy(
    host: HostId,
    source: string,
    destination: string,
    signal: AbortSignal,
  ): Promise<CreatedProject>;
}

/** Create, copy, and inspect. Each call uses a new request id, starting at `project-1`. */
export function registryProjects(ports: RegistryProjectPorts): RegistryProjects {
  let next = 1;
  const requestId = (): string => {
    const id = `project-${next}`;
    next += 1;
    return id;
  };
  return {
    async destination(host, example, destination, signal) {
      const response = await callInspectDestination(
        await readTransport(ports, host, signal),
        acceptInspectDestination({requestId: requestId(), example, destination}),
        signal,
      );
      const projected = projectMessage(InspectDestinationResponseSchema, response) as {
        state: ExampleDestination;
        project: string | null;
      };
      return {state: projected.state, project: projected.project};
    },
    async create(host, example, destination, signal) {
      const response = await callCreateExample(
        await mutateTransport(ports, host, signal),
        acceptCreateExample({requestId: requestId(), example, destination}),
        signal,
      );
      return creation(response);
    },
    async copy(host, source, destination, signal) {
      const response = await callCopyProject(
        await mutateTransport(ports, host, signal),
        acceptCopyProject({requestId: requestId(), source, destination}),
        signal,
      );
      const projected = projectMessage(CopyProjectResponseSchema, response) as {
        created: CreatedProject;
      };
      return projected.created;
    },
  };
}

function creation(
  response: CreateExampleResponse,
): {kind: 'created'; created: CreatedProject} | {kind: 'taken'} {
  const result = response.result;
  if (result.case === 'created') {
    return {
      kind: 'created',
      created: projectMessage(CreatedProjectSchema, result.value) as CreatedProject,
    };
  }
  if (result.case === 'taken') return {kind: 'taken'};
  throw new Error('project create has no result');
}

async function readTransport(ports: RegistryProjectPorts, host: HostId, signal: AbortSignal) {
  const checkout = requireCheckout(await ports.checkout(host), null);
  if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
  return registryTransport(checkout, (bytes, owned) => ports.read(host, bytes, owned));
}

async function mutateTransport(ports: RegistryProjectPorts, host: HostId, signal: AbortSignal) {
  const checkout = requireCheckout(await ports.checkout(host), null);
  if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
  return {
    async invoke(id: number, payload: Uint8Array, _totalMs: number, owned: OperationSignal) {
      if (!(owned instanceof AbortSignal)) throw new Error('operation cancelled');
      const policy = Object.values(operationPolicies).find(item => item.id === id);
      if (policy === undefined) throw new Error('unknown mutation');
      return decodeHostMutate(
        policy.name,
        await ports.mutate(host, encodeHostMutate(checkout, policy.name, payload), owned),
      );
    },
  };
}
