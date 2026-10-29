import {describe, expect, test} from 'bun:test';
import type {HarnessChoice, HarnessOption} from '@vibesys/contracts/host-helper';
import {FakeClock} from '@vibesys/transport/testing';
import {FAKE_MODEL_CATALOGUE, FakeLink} from '../testing/index.js';
import {HelperRequestError} from './helper-channel.js';
import {HostConnection} from './host-connection.js';

const CONFIG = {delaysMs: [500], deadlineMs: 60_000, heartbeatMs: 15_000};
const PROJECT = '/p';

function connected(): {link: FakeLink; connection: HostConnection} {
  const link = new FakeLink();
  const clock = new FakeClock();
  link.helper.host.projects.set(PROJECT, ['spsc']);
  link.helper.host.harnesses.splice(0, link.helper.host.harnesses.length, ...FAKE_MODEL_CATALOGUE);
  const connection = new HostConnection({
    link,
    checkout: '/src/vibesys',
    appVersion: 'test',
    config: CONFIG,
    scheduleTimeout: clock.scheduleTimeout,
    log: () => {},
  });
  return {link, connection};
}

function pair(option: HarnessOption): HarnessChoice {
  return {provider: option.provider, model: option.model};
}

async function failure(promise: Promise<unknown>): Promise<HelperRequestError> {
  try {
    await promise;
  } catch (error) {
    if (error instanceof HelperRequestError) return error;
    throw error;
  }
  throw new Error('resolved instead of rejecting');
}

const VETTED = FAKE_MODEL_CATALOGUE.map(pair);
const ROLES = ['multi-orchestrator', 'multi-implementer'] as const;

describe('the Fake helper offers generations to a client that asks', () => {
  test('and the plain list to one that does not', async () => {
    const {connection} = connected();
    await using _connection = connection;

    const asked = await connection.request({type: 'harnesses.list', with_generations: true});
    const plain = await connection.request({type: 'harnesses.list'});

    expect(asked).toMatchObject({harnesses: FAKE_MODEL_CATALOGUE});
    if (plain.type !== 'harnesses.list') throw new Error('not a harness list');
    expect(plain.harnesses).toHaveLength(FAKE_MODEL_CATALOGUE.length);
    for (const option of plain.harnesses) {
      expect(option.display?.maker).toBeUndefined();
      expect(option.display?.generation).toBeUndefined();
      expect(option.display?.model_name).toBeDefined();
    }
  });

  test('groups each maker into generations whose order is distinct and newest first', () => {
    const orders = new Map<string, Map<string, number>>();
    for (const option of FAKE_MODEL_CATALOGUE) {
      const {maker, generation} = option.display ?? {};
      if (maker == null || generation == null) continue;
      const made = orders.get(maker) ?? new Map<string, number>();
      expect(made.get(generation.id) ?? generation.order).toBe(generation.order);
      made.set(generation.id, generation.order);
      orders.set(maker, made);
    }
    for (const made of orders.values()) {
      const ranks = [...made.values()];
      expect(new Set(ranks).size).toBe(ranks.length);
      expect(Math.min(...ranks)).toBe(1);
    }
  });
});

describe('the Fake helper takes an agent per role as the helper does', () => {
  for (const type of ['run.start', 'run.preflight'] as const) {
    const request = (agents: Record<string, HarnessChoice>, extra: object = {}) =>
      ({type, project: PROJECT, agents, ...extra}) as const;

    test(`${type}: every vetted pair for every declared role is accepted`, async () => {
      const {connection} = connected();
      await using _connection = connection;
      for (const choice of VETTED) {
        for (const role of ROLES) {
          const reply = await connection.request(request({[role]: choice}));
          expect(reply.type).toBe(type);
        }
      }
    });

    test(`${type}: a pair that is not offered is refused naming the role's key`, async () => {
      const {connection} = connected();
      await using _connection = connection;
      const unoffered: HarnessChoice[] = [
        {provider: 'codex', model: 'gpt-z'},
        {provider: 'claude', model: 'gpt-6-sol'},
        {provider: 'gemini', model: 'gemini-a'},
      ];
      for (const choice of unoffered) {
        const error = await failure(connection.request(request({'multi-judge': choice})));
        expect(error.code).toBe('unvetted_harness');
        expect(error.message).toContain('agents.multi-judge');
      }
    });

    test(`${type}: a role the run does not declare is refused naming the key`, async () => {
      const {connection} = connected();
      await using _connection = connection;
      const choice = VETTED[0] as HarnessChoice;

      const error = await failure(
        connection.request(request({ghost: choice, 'multi-judge': choice})),
      );

      expect(error.code).toBe('invalid_request');
      expect(error.message).toContain('agents.ghost');
      expect(error.message).not.toContain('agents.multi-judge');
    });

    test(`${type}: a role's typed model and its agent both choosing its model is refused`, async () => {
      const {connection} = connected();
      await using _connection = connection;
      const choice = VETTED[0] as HarnessChoice;

      const error = await failure(
        connection.request(
          request(
            {'multi-judge': choice},
            {options: {roles: [{role: 'multi-judge', model: 'gpt-a', reasoning_effort: 'high'}]}},
          ),
        ),
      );

      expect(error.code).toBe('invalid_request');
      expect(error.message).toContain('agents.multi-judge');
      expect(error.message).toContain('options.roles');
    });

    test(`${type}: args may not repeat the role flags that agents set`, async () => {
      const {connection} = connected();
      await using _connection = connection;
      const choice = VETTED[0] as HarnessChoice;

      for (const token of ['--role-provider=x=y', '--role-m=x=y']) {
        const error = await failure(
          connection.request(request({'multi-judge': choice}, {args: [token]})),
        );
        expect(error.code).toBe('invalid_request');
        expect(error.message).toContain(token);
      }
    });
  }

  test('the helper does not take agents before hello offered the feature', async () => {
    const {link, connection} = connected();
    link.helper.features.delete('run_role_agents');
    await using _connection = connection;

    const error = await failure(
      connection.request({
        type: 'run.start',
        project: PROJECT,
        agents: {'multi-judge': VETTED[0] as HarnessChoice},
      }),
    );

    expect(error.code).toBe('feature_not_negotiated');
    expect(link.helper.requests.some(request => request.type === 'run.start')).toBe(false);
  });
});
