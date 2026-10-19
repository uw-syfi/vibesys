import {describe, expect, it} from 'bun:test';
import {parseHelperMessage, parseHelperRequest} from '@vibesys/contracts/host-helper';
import {
  clusterTestPassed,
  clusterTestPassedUntyped,
  clusterTestsFailed,
  computeSettings,
  computeSettingsEmpty,
  loginNodeCluster,
  measurementChecks,
  operatorCluster,
  sshCluster,
} from './compute.js';

const reply = (type: string, body: Record<string, unknown>): string =>
  JSON.stringify({type, request_id: 'r1', ...body});
const request = (type: string, body: Record<string, unknown>): string =>
  JSON.stringify({type, request_id: 'r1', deadline_ms: 5000, ...body});

describe('the compute fixtures', () => {
  it('are what a helper that offers machine_compute may answer', () => {
    for (const settings of [computeSettings, computeSettingsEmpty]) {
      for (const type of ['compute.list', 'compute.add_cluster', 'compute.remove_cluster']) {
        expect(() => parseHelperMessage(reply(type, {settings}))).not.toThrow();
      }
    }
    for (const result of [clusterTestPassed, clusterTestPassedUntyped, ...clusterTestsFailed]) {
      expect(() => parseHelperMessage(reply('compute.test_cluster', {result}))).not.toThrow();
    }
  });

  it('are the cluster a client sends to test or add', () => {
    for (const cluster of [sshCluster, loginNodeCluster, operatorCluster]) {
      for (const type of ['compute.test_cluster', 'compute.add_cluster']) {
        expect(() => parseHelperRequest(request(type, {cluster}))).not.toThrow();
      }
    }
    expect(() => parseHelperRequest(request('compute.list', {}))).not.toThrow();
    expect(() =>
      parseHelperRequest(request('compute.remove_cluster', {name: 'campus'})),
    ).not.toThrow();
  });

  it('cover every kind of target and the checks measure_on adds to a preflight', () => {
    for (const measure_on of [
      {kind: 'same_machine'},
      {kind: 'slurm', cluster: 'lab-h100'},
      {kind: 'modal'},
    ]) {
      for (const type of ['run.start', 'run.preflight']) {
        expect(() => parseHelperRequest(request(type, {project: '/p', measure_on}))).not.toThrow();
      }
    }
    const checks = Object.values(measurementChecks);
    expect(() => parseHelperMessage(reply('run.preflight', {checks}))).not.toThrow();
  });

  it('are strict: an unknown key in a cluster or a target is a parse error', () => {
    const cluster = {...sshCluster, extra: 1};
    expect(() => parseHelperRequest(request('compute.add_cluster', {cluster}))).toThrow();
    const measure_on = {kind: 'modal', cluster: 'x'};
    expect(() => parseHelperRequest(request('run.start', {project: '/p', measure_on}))).toThrow();
  });

  it('name each listed cluster once and never carry a secret', () => {
    const names = computeSettings.clusters.map(listed => listed.cluster.name);
    expect(new Set(names).size).toBe(names.length);
    expect(JSON.stringify(computeSettings)).not.toMatch(/secret/i);
    expect(computeSettings.clusters.some(listed => listed.source === 'slurm_toml')).toBe(true);
  });
});
