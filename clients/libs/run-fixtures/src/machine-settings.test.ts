import {describe, expect, test} from 'bun:test';
import {parseHelperMessage} from '@vibesys/contracts/host-helper';
import {
  clusterTestsByAlias,
  loginStartsByProvider,
  machineSettingsWorld,
} from './machine-settings.js';

describe('the machine settings world', () => {
  test('it has a machine set up, one empty, and one whose helper predates both features', () => {
    const {local, 'ssh:host-b': empty, 'ssh:host-c': old} = machineSettingsWorld;
    expect(local?.compute?.clusters.length).toBeGreaterThan(0);
    expect(local?.compute?.modal.token_present).toBe(true);
    expect(local?.logins?.map(login => login.state)).toContain('signed_out');
    expect(empty?.compute?.clusters).toEqual([]);
    expect(empty?.logins?.filter(login => login.installed)).toHaveLength(1);
    expect(old).toEqual({compute: null, logins: null});
  });

  test('every answer is what a helper that offers the feature may send', () => {
    const reply = (type: string, body: Record<string, unknown>): string =>
      JSON.stringify({type, request_id: 'r1', ...body});
    for (const machine of Object.values(machineSettingsWorld)) {
      if (machine.compute !== null) {
        parseHelperMessage(reply('compute.list', {settings: machine.compute}));
      }
      if (machine.logins !== null)
        parseHelperMessage(reply('agent_logins.list', {logins: machine.logins}));
    }
    for (const result of Object.values(clusterTestsByAlias)) {
      parseHelperMessage(reply('compute.test_cluster', {result}));
    }
    for (const result of Object.values(loginStartsByProvider)) {
      parseHelperMessage(reply('agent_logins.start', {result}));
    }
  });

  test('every login a start answers for is one the world lists', () => {
    const providers = new Set(
      Object.values(machineSettingsWorld)
        .flatMap(machine => machine.logins ?? [])
        .map(login => login.provider),
    );
    for (const provider of Object.keys(loginStartsByProvider))
      expect(providers.has(provider)).toBe(true);
  });

  test('a test of a reachable cluster passes, and each stage has a failure', () => {
    expect(clusterTestsByAlias['lab-login']?.outcome).toBe('passed');
    const stages = Object.values(clusterTestsByAlias).flatMap(result =>
      result.outcome === 'failed' ? [result.stage] : [],
    );
    expect(stages.sort()).toEqual(['scheduler', 'workspace']);
  });
});
