/** Registry cluster changes behind the run-transport switch.

Budgets stay in the generated registry. The helper JSON channel is the off switch.
*/
import type {
  ClusterHardware,
  ClusterTestFailed,
  ClusterTestPassed,
  ComputeSettings,
  SlurmClusterSettings,
} from '@vibesys/contracts/host-helper';
import type {HostId} from '@vibesys/hosts';
import {
  Accelerator,
  acceptPutCluster,
  acceptRemoveCluster,
  acceptTestCluster,
  ClusterTestFailedSchema,
  ClusterTestPassedSchema,
  callPutCluster,
  callRemoveCluster,
  callTestCluster,
  decodeHostMutate,
  encodeHostMutate,
  type OperationSignal,
  operationPolicies,
  PutClusterResponseSchema,
  projectMessage,
  RemoveClusterResponseSchema,
  type TestClusterResponse,
} from '@vibesys/proto';
import {requireCheckout} from './registry-reads.js';

export interface RegistryClusterPorts {
  checkout(host: HostId): Promise<string | null>;
  unary(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
}

export interface RegistryClusters {
  put(host: HostId, cluster: SlurmClusterSettings, signal: AbortSignal): Promise<ComputeSettings>;
  remove(host: HostId, name: string, signal: AbortSignal): Promise<ComputeSettings>;
  test(
    host: HostId,
    cluster: SlurmClusterSettings,
    signal: AbortSignal,
  ): Promise<ClusterTestPassed | ClusterTestFailed>;
}

const ACCELERATORS = {
  cpu: Accelerator.CPU,
  cuda: Accelerator.CUDA,
  rocm: Accelerator.ROCM,
  metal: Accelerator.METAL,
} as const;

/** Put, remove, and test. Each call uses a new request id, starting at `cluster-1`. */
export function registryClusters(ports: RegistryClusterPorts): RegistryClusters {
  let next = 1;
  const requestId = (): string => {
    const id = `cluster-${next}`;
    next += 1;
    return id;
  };
  return {
    async put(host, cluster, signal) {
      const response = await callPutCluster(
        await transport(ports, host, signal),
        acceptPutCluster({requestId: requestId(), cluster: clusterMessage(cluster)}),
        signal,
      );
      return settingsOf(PutClusterResponseSchema, response);
    },
    async remove(host, name, signal) {
      const response = await callRemoveCluster(
        await transport(ports, host, signal),
        acceptRemoveCluster({requestId: requestId(), name}),
        signal,
      );
      return settingsOf(RemoveClusterResponseSchema, response);
    },
    async test(host, cluster, signal) {
      const response = await callTestCluster(
        await transport(ports, host, signal),
        acceptTestCluster({requestId: requestId(), cluster: clusterMessage(cluster)}),
        signal,
      );
      return testOf(response);
    },
  };
}

async function transport(ports: RegistryClusterPorts, host: HostId, signal: AbortSignal) {
  const checkout = requireCheckout(await ports.checkout(host), null);
  if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
  return {
    async invoke(id: number, payload: Uint8Array, _totalMs: number, owned: OperationSignal) {
      if (!(owned instanceof AbortSignal)) throw new Error('operation cancelled');
      const policy = Object.values(operationPolicies).find(item => item.id === id);
      if (policy === undefined) throw new Error('unknown mutation');
      return decodeHostMutate(
        policy.name,
        await ports.unary(host, encodeHostMutate(checkout, policy.name, payload), owned),
      );
    },
  };
}

function settingsOf(
  schema: typeof PutClusterResponseSchema | typeof RemoveClusterResponseSchema,
  response: unknown,
): ComputeSettings {
  const projected = projectMessage(schema, response as never) as {settings: ComputeSettings};
  return projected.settings;
}

function testOf(response: TestClusterResponse): ClusterTestPassed | ClusterTestFailed {
  const result = response.result;
  if (result.case === 'passed') {
    const body = projectMessage(ClusterTestPassedSchema, result.value) as Omit<
      ClusterTestPassed,
      'outcome'
    >;
    return {outcome: 'passed', hardware: body.hardware, partitions: body.partitions};
  }
  if (result.case === 'failed') {
    const body = projectMessage(ClusterTestFailedSchema, result.value) as Omit<
      ClusterTestFailed,
      'outcome'
    >;
    return {outcome: 'failed', stage: body.stage, detail: body.detail};
  }
  throw new Error('cluster test has no result');
}

function clusterMessage(settings: SlurmClusterSettings) {
  return {
    name: settings.name,
    reach: reach(settings),
    workspace: settings.workspace,
    ...(settings.partition ? {partition: settings.partition} : {}),
    ...(settings.nodes == null ? {} : {nodes: BigInt(settings.nodes)}),
    ...(settings.time_limit ? {timeLimit: settings.time_limit} : {}),
    ...(settings.sbatch_options === undefined ? {} : {sbatchOptions: [...settings.sbatch_options]}),
    ...clusterHardware(settings.hardware),
  };
}

function reach(settings: SlurmClusterSettings) {
  if (settings.reach.kind === 'ssh') {
    return {value: {case: 'sshReach' as const, value: {kind: 'ssh', host: settings.reach.host}}};
  }
  return {value: {case: 'loginNodeReach' as const, value: {kind: 'login_node'}}};
}

function clusterHardware(hardware: ClusterHardware | null | undefined): {
  hardware?: {
    accelerator?: Accelerator;
    gpuModels: string[];
    gpusPerNode: bigint;
  };
} {
  if (hardware == null) return {};
  const accelerator = hardware.accelerator;
  return {
    hardware: {
      ...(accelerator == null ? {} : {accelerator: ACCELERATORS[accelerator]}),
      gpuModels: [...(hardware.gpu_models ?? [])],
      gpusPerNode: BigInt(hardware.gpus_per_node ?? 0),
    },
  };
}
