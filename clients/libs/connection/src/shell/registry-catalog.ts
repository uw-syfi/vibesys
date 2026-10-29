/** New-run catalogs and preflight over the registry, without opening a helper channel. */
import type {MessageInitShape} from '@bufbuild/protobuf';
import type {
  AgentLogin,
  Backend,
  Check,
  ExampleList,
  HarnessesListReply,
  TaskGoal,
} from '@vibesys/contracts/host-helper';
import type {HostId} from '@vibesys/hosts';
import {
  acceptGetTaskGoal,
  acceptListAgentLogins,
  acceptListExamples,
  acceptListHarnesses,
  acceptListTasks,
  acceptPreflightRun,
  callGetTaskGoal,
  callListAgentLogins,
  callListExamples,
  callListHarnesses,
  callListTasks,
  callPreflightRun,
  GetTaskGoalResponseSchema,
  ListAgentLoginsResponseSchema,
  ListExamplesResponseSchema,
  ListHarnessesResponseSchema,
  type PreflightRunRequestSchema,
  PreflightRunResponseSchema,
  projectMessage,
  ReasoningEffort,
  RunResults,
} from '@vibesys/proto';
import {type RegistryReadPorts, registryTransport, requireCheckout} from './registry-reads.js';
import type {RegistryRunInputs} from './registry-start.js';

export interface RegistryRunCatalog {
  logins(host: HostId, signal: AbortSignal): Promise<AgentLogin[]>;
  tasks(host: HostId, project: string, signal: AbortSignal): Promise<string[]>;
  harnesses(host: HostId, signal: AbortSignal): Promise<HarnessesListReply['harnesses']>;
  examples(host: HostId, signal: AbortSignal): Promise<ExampleList>;
  taskGoal(host: HostId, project: string, task: string, signal: AbortSignal): Promise<TaskGoal>;
  preflight(
    host: HostId,
    inputs: RegistryRunInputs,
    signal: AbortSignal,
  ): Promise<{
    checks: Check[];
    roles: string[] | null;
    backend: Backend | null;
  }>;
}

/** Generated operation calls own budgets, validation, refusal categories, and correlation. */
export function registryRunCatalog(ports: RegistryReadPorts): RegistryRunCatalog {
  async function transport(host: HostId, signal: AbortSignal) {
    const checkout = requireCheckout(await ports.checkout(host), null);
    if (signal.aborted) throw new Error('operation cancelled', {cause: signal.reason});
    return registryTransport(checkout, (bytes, owned) => ports.unary(host, bytes, owned));
  }
  return {
    async logins(host, signal) {
      const reply = await callListAgentLogins(
        await transport(host, signal),
        acceptListAgentLogins({requestId: 'catalog'}),
        signal,
      );
      return (projectMessage(ListAgentLoginsResponseSchema, reply) as {logins: AgentLogin[]})
        .logins;
    },
    async tasks(host, project, signal) {
      const reply = await callListTasks(
        await transport(host, signal),
        acceptListTasks({requestId: 'catalog', project}),
        signal,
      );
      if (reply.tasks === undefined) throw new Error('task response is missing its catalog');
      return reply.tasks.tasks.map(task => task.name);
    },
    async harnesses(host, signal) {
      const reply = await callListHarnesses(
        await transport(host, signal),
        acceptListHarnesses({requestId: 'catalog', withGenerations: true}),
        signal,
      );
      return (
        projectMessage(ListHarnessesResponseSchema, reply) as {
          harnesses: HarnessesListReply['harnesses'];
        }
      ).harnesses;
    },
    async examples(host, signal) {
      const reply = await callListExamples(
        await transport(host, signal),
        acceptListExamples({requestId: 'catalog', withCatalogue: true}),
        signal,
      );
      return (projectMessage(ListExamplesResponseSchema, reply) as {listing: ExampleList}).listing;
    },
    async taskGoal(host, project, task, signal) {
      const reply = await callGetTaskGoal(
        await transport(host, signal),
        acceptGetTaskGoal({requestId: 'catalog', project, task}),
        signal,
      );
      return (projectMessage(GetTaskGoalResponseSchema, reply) as {goal: TaskGoal}).goal;
    },
    async preflight(host, inputs, signal) {
      const reply = await callPreflightRun(
        await transport(host, signal),
        acceptPreflightRun(preflightRequest(inputs)),
        signal,
      );
      const {checks, roles, backend} = projectMessage(PreflightRunResponseSchema, reply) as {
        checks: Check[];
        roles: string[] | null;
        backend: Backend | null;
      };
      return {checks, roles, backend};
    },
  };
}

const REASONING = {
  default: ReasoningEffort.DEFAULT,
  low: ReasoningEffort.LOW,
  medium: ReasoningEffort.MEDIUM,
  high: ReasoningEffort.HIGH,
} as const;

function preflightRequest(
  inputs: RegistryRunInputs,
): MessageInitShape<typeof PreflightRunRequestSchema> {
  const options = inputs.options;
  const measure = inputs.measure_on;
  return {
    requestId: 'catalog',
    project: inputs.project,
    task: inputs.task,
    args: [...inputs.args],
    ...(inputs.harness === null ? {} : {harness: {...inputs.harness}}),
    options: {
      ...(options.rounds == null ? {} : {rounds: BigInt(options.rounds)}),
      ...(options.time_limit_s == null ? {} : {timeLimitS: BigInt(options.time_limit_s)}),
      ...(options.results == null
        ? {}
        : {results: options.results === 'keep' ? RunResults.KEEP : RunResults.PUBLISH_GITHUB}),
      roles: (options.roles ?? []).map(role => ({
        role: role.role,
        ...(role.model == null ? {} : {model: role.model}),
        reasoningEffort: REASONING[role.reasoning_effort ?? 'default'],
      })),
    },
    agents: {
      items: Object.fromEntries(
        Object.entries(inputs.agents).map(([role, harness]) => [role, {...harness}]),
      ),
    },
    measureOn: {
      value:
        measure.kind === 'slurm'
          ? {case: 'slurmTarget', value: {kind: 'slurm', cluster: measure.cluster}}
          : measure.kind === 'modal'
            ? {case: 'modalTarget', value: {kind: 'modal'}}
            : {case: 'sameMachine', value: {kind: 'same_machine'}},
    },
    withRoles: true,
    withBackend: true,
    withProjectClean: true,
    withCauses: true,
  };
}
