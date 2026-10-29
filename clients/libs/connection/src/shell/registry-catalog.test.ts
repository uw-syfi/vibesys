import {expect, test} from 'bun:test';
import {
  AgentLoginState,
  Backend,
  CheckId,
  CheckState,
  ReasoningEffort,
  RunResults,
} from '@vibesys/proto';
import {type RegistryRunInputs, registryRunCatalog} from '../index.js';
import {FakeReadCatalog} from '../testing/index.js';

const HOST = {kind: 'local'} as const;
const INPUTS: RegistryRunInputs = {
  project: '/project',
  task: 'queue',
  args: ['--size', '8'],
  harness: {provider: 'codex', model: 'model-a'},
  options: {
    rounds: 2,
    time_limit_s: 90,
    results: 'publish_github',
    roles: [{role: 'coder', model: 'model-b', reasoning_effort: 'high'}],
  },
  agents: {coder: {provider: 'claude', model: 'model-b'}},
  measure_on: {kind: 'slurm', cluster: 'cluster-a'},
};

test('preflight preserves every input and asks for complete check metadata', async () => {
  const peer = new FakeReadCatalog();
  peer.preflightResult.roles = {
    $typeName: 'vibesys.host.v1.PreflightRunResponseRoles',
    items: ['coder'],
  };
  peer.preflightResult.backend = Backend.CPU;
  peer.preflightResult.checks.push({
    $typeName: 'vibesys.host.v1.Check',
    id: CheckId.SETTINGS_VALID,
    state: CheckState.OK,
    detail: 'Ready',
  });
  const result = await registryRunCatalog(peer).preflight(
    HOST,
    INPUTS,
    new AbortController().signal,
  );
  expect(result).toMatchObject({
    roles: ['coder'],
    backend: 'cpu',
    checks: [{id: 'settings_valid', state: 'ok', detail: 'Ready'}],
  });
  const request = peer.requests[0]?.method;
  expect(request?.case).toBe('preflightRun');
  if (request?.case !== 'preflightRun') throw new Error('missing preflight');
  expect(request.value).toMatchObject({
    project: '/project',
    task: 'queue',
    args: INPUTS.args,
    harness: INPUTS.harness,
    options: {
      rounds: 2n,
      timeLimitS: 90n,
      results: RunResults.PUBLISH_GITHUB,
      roles: [{role: 'coder', model: 'model-b', reasoningEffort: ReasoningEffort.HIGH}],
    },
    agents: {items: INPUTS.agents},
    measureOn: {value: {case: 'slurmTarget', value: {kind: 'slurm', cluster: 'cluster-a'}}},
    withRoles: true,
    withBackend: true,
    withProjectClean: true,
    withCauses: true,
  });
});

test('preflight covers optional options, all measurement targets and reasoning choices', async () => {
  const peer = new FakeReadCatalog();
  const reads = registryRunCatalog(peer);
  for (const measure_on of [
    {kind: 'same_machine'},
    {kind: 'modal'},
    {kind: 'slurm', cluster: 'cluster-a'},
  ] as const) {
    for (const reasoning_effort of ['default', 'low', 'medium', 'high'] as const) {
      await reads.preflight(
        HOST,
        {
          ...INPUTS,
          harness: null,
          agents: {},
          measure_on,
          options: {roles: [{role: 'coder', reasoning_effort}]},
        },
        new AbortController().signal,
      );
    }
  }
  expect(peer.requests.length).toBe(12);
});

test('catalogs retain details, generation metadata and task identity', async () => {
  const peer = new FakeReadCatalog();
  peer.projects.set('/project', ['queue', 'sort']);
  peer.goals.set('/project/queue', {
    task: 'queue',
    goal: 'Improve throughput',
    objectives: [{metric: 'throughput', direction: 'maximize'}],
    goalTruncated: true,
  });
  peer.harnessOptions = [
    {
      provider: 'codex',
      model: 'model-a',
      default: true,
      display: {providerName: 'Codex', generation: {id: 'gen-a', name: 'A', order: 1n}},
    },
  ];
  peer.exampleList = {
    version: 1n,
    checkout: '/checkout',
    examples: [{name: 'queue', category: 'systems', tasks: ['queue'], externalRepo: true}],
  };
  const reads = registryRunCatalog(peer);
  const signal = new AbortController().signal;
  expect(await reads.tasks(HOST, '/project', signal)).toEqual(['queue', 'sort']);
  expect(await reads.taskGoal(HOST, '/project', 'queue', signal)).toMatchObject({
    task: 'queue',
    goal: 'Improve throughput',
    goal_truncated: true,
  });
  expect(await reads.harnesses(HOST, signal)).toMatchObject([
    {display: {provider_name: 'Codex', generation: {id: 'gen-a', name: 'A', order: 1}}},
  ]);
  expect(await reads.examples(HOST, signal)).toMatchObject({
    examples: [{name: 'queue', external_repo: true}],
  });
  expect(peer.requests.map(request => request.checkout)).toEqual(Array(4).fill('/checkout'));
  expect(peer.requests[2]?.method).toMatchObject({
    case: 'listHarnesses',
    value: {withGenerations: true},
  });
  expect(peer.requests[3]?.method).toMatchObject({
    case: 'listExamples',
    value: {withCatalogue: true},
  });
});

test('catalog cancellation, missing checkout and peer failure never turn into empty success', async () => {
  const peer = new FakeReadCatalog();
  const reads = registryRunCatalog(peer);
  const abort = new AbortController();
  abort.abort();
  await expect(reads.examples(HOST, abort.signal)).rejects.toThrow('cancelled');
  peer.savedCheckout = null;
  await expect(reads.tasks(HOST, '/project', new AbortController().signal)).rejects.toThrow(
    'Set a checkout first',
  );
  peer.savedCheckout = '/checkout';
  peer.failure = new Error('denied');
  await expect(reads.harnesses(HOST, new AbortController().signal)).rejects.toThrow('denied');
  expect(peer.requests).toEqual([]);
});

test('login status preserves provider, credential-source and missing-CLI facts', async () => {
  const peer = new FakeReadCatalog();
  peer.loginFacts = [
    {
      provider: 'agent-a',
      providerName: 'Agent A',
      installed: true,
      state: AgentLoginState.SIGNED_IN,
      how: {value: {case: 'account', value: {kind: 'account'}}},
      apiKeyVariables: ['AGENT_API_KEY'],
      detail: 'Signed in',
      canStartLogin: true,
    },
    {
      provider: 'agent-b',
      providerName: 'Agent B',
      installed: true,
      state: AgentLoginState.SIGNED_IN,
      how: {value: {case: 'apiKey', value: {kind: 'api_key', variable: 'AGENT_API_KEY'}}},
    },
    {provider: 'agent-c', providerName: 'Agent C', state: AgentLoginState.UNKNOWN},
  ];
  expect(await registryRunCatalog(peer).logins(HOST, new AbortController().signal)).toMatchObject([
    {
      provider: 'agent-a',
      provider_name: 'Agent A',
      installed: true,
      state: 'signed_in',
      how: {kind: 'account'},
      api_key_variables: ['AGENT_API_KEY'],
      detail: 'Signed in',
      can_start_login: true,
    },
    {provider: 'agent-b', state: 'signed_in', how: {kind: 'api_key', variable: 'AGENT_API_KEY'}},
    {provider: 'agent-c', installed: false, state: 'unknown', how: null},
  ]);
  expect(peer.requests[0]?.method.case).toBe('listAgentLogins');
});
