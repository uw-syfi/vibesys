/** Registry starts behind the run-transport switch.

The gateway keeps an admitted start running after this call's signal aborts.
Budgets stay in the generated registry.
*/
import type {HostId} from '@vibesys/hosts';
import {
  acceptStartRun,
  callStartRun,
  decodeHostMutate,
  encodeHostMutate,
  operationPolicies,
  StartRunReasoning,
  StartRunResults,
} from '@vibesys/proto';
import {requireCheckout} from './registry-reads.js';

export interface RegistryRunInputs {
  readonly project: string;
  readonly task: string;
  readonly harness: {readonly provider: string; readonly model: string} | null;
  readonly options: {
    readonly rounds?: number | null;
    readonly time_limit_s?: number | null;
    readonly results?: 'keep' | 'publish_github' | null;
    readonly roles?: readonly {
      readonly role: string;
      readonly model?: string | null;
      readonly reasoning_effort?: 'default' | 'low' | 'medium' | 'high';
    }[];
  };
  readonly agents: Readonly<Record<string, {readonly provider: string; readonly model: string}>>;
  readonly measure_on:
    | {readonly kind: 'same_machine'}
    | {readonly kind: 'slurm'; readonly cluster: string}
    | {readonly kind: 'modal'};
  readonly args: readonly string[];
}

export interface RegistryStartPorts {
  checkout(host: HostId): Promise<string | null>;
  unary(host: HostId, bytes: Uint8Array, signal: AbortSignal): Promise<Uint8Array>;
}

/** One admitted start. The request id is stable for this object, starting at `start-1`. */
export function registryStart(ports: RegistryStartPorts): {
  start(
    host: HostId,
    inputs: RegistryRunInputs,
    signal: AbortSignal,
  ): Promise<{runId: string; instanceId: string}>;
} {
  let next = 1;
  return {
    async start(host, inputs, signal) {
      const requestId = `start-${next}`;
      next += 1;
      const checkout = requireCheckout(await ports.checkout(host), null);
      if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
      const response = await callStartRun(
        {
          async invoke(id, payload, _totalMs, owned) {
            if (!(owned instanceof AbortSignal)) throw new Error('operation cancelled');
            const policy = Object.values(operationPolicies).find(item => item.id === id);
            if (policy === undefined) throw new Error('unknown mutation');
            return decodeHostMutate(
              policy.name,
              await ports.unary(host, encodeHostMutate(checkout, policy.name, payload), owned),
            );
          },
        },
        acceptStartRun(startRequest(requestId, inputs)),
        signal,
      );
      return {runId: response.runId, instanceId: response.instanceId};
    },
  };
}

function startRequest(requestId: string, inputs: RegistryRunInputs) {
  return {
    requestId,
    project: inputs.project,
    task: inputs.task,
    args: [...inputs.args],
    ...(inputs.harness === null ? {} : {harness: inputs.harness}),
    ...startOptions(inputs),
    ...startAgents(inputs),
    ...startMeasure(inputs),
  };
}

function startOptions(inputs: RegistryRunInputs): {options?: object} {
  const options = inputs.options;
  const roles = (options.roles ?? []).map(role => ({
    role: role.role,
    ...(role.model == null || role.model === '' ? {} : {model: role.model}),
    reasoning: reasoning(role.reasoning_effort),
  }));
  const results =
    options.results === 'keep'
      ? StartRunResults.KEEP
      : options.results === 'publish_github'
        ? StartRunResults.PUBLISH_GITHUB
        : undefined;
  if (
    options.rounds == null &&
    options.time_limit_s == null &&
    results === undefined &&
    roles.length === 0
  ) {
    return {};
  }
  return {
    options: {
      ...(options.rounds == null ? {} : {rounds: options.rounds}),
      ...(options.time_limit_s == null ? {} : {timeLimitS: options.time_limit_s}),
      ...(results === undefined ? {} : {results}),
      ...(roles.length === 0 ? {} : {roles}),
    },
  };
}

function reasoning(effort: 'default' | 'low' | 'medium' | 'high' | undefined): StartRunReasoning {
  switch (effort) {
    case 'low':
      return StartRunReasoning.LOW;
    case 'medium':
      return StartRunReasoning.MEDIUM;
    case 'high':
      return StartRunReasoning.HIGH;
    default:
      return StartRunReasoning.DEFAULT;
  }
}

function startAgents(inputs: RegistryRunInputs): {agents?: object[]} {
  const agents = Object.entries(inputs.agents).map(([role, harness]) => ({role, harness}));
  return agents.length === 0 ? {} : {agents};
}

function startMeasure(inputs: RegistryRunInputs): {measureOn?: object} {
  switch (inputs.measure_on.kind) {
    case 'slurm':
      return {measureOn: {target: {case: 'slurmCluster', value: inputs.measure_on.cluster}}};
    case 'modal':
      return {measureOn: {target: {case: 'modal', value: {}}}};
    case 'same_machine':
      return {};
  }
}
