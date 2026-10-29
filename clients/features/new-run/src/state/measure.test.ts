import {describe, expect, test} from 'bun:test';
import type {ComputeSettings, MeasureTarget} from '@vibesys/ipc';
import {SEEDS, Seeded} from '@vibesys/store/testing';
import {type Form, prefill, runInputs, SAME_MACHINE} from './form.js';
import {
  type Compute,
  formWithMeasure,
  measureOptions,
  nothingSetUp,
  sameTarget,
  targetFacts,
} from './measure.js';

const NAMES = ['lab-h100', 'cluster-m', 'cluster b'];

/** A machine's Settings data with arbitrary clusters and Modal token. */
function arbitrary(random: Seeded): ComputeSettings {
  const names = NAMES.filter(() => random.bool());
  return {
    config_path: '/home/me/.config/vibesys/config.toml',
    clusters: names.map(name => ({
      cluster: {name, reach: {kind: 'login_node'}, workspace: '/scratch/me'},
      source: 'user',
    })) as unknown as ComputeSettings['clusters'],
    modal: {token_present: random.bool()},
  } as ComputeSettings;
}

function formMeasuring(measure: MeasureTarget): Form {
  return {
    ...prefill('local', []),
    project: {source: 'recent', path: '/p'},
    task: 'mpmc',
    agent: {provider: 'claude', model: 'm', roles: []},
    measure,
  };
}

describe('where results are measured', () => {
  test('a machine offers itself, then each cluster set up on it, then Modal only with a token', () => {
    for (const seed of SEEDS) {
      const settings = arbitrary(new Seeded(seed));
      const options = measureOptions({kind: 'offered', value: settings});
      expect(options[0]).toEqual(SAME_MACHINE);
      expect(options.filter(each => each.kind === 'slurm')).toEqual(
        settings.clusters.map(listed => ({kind: 'slurm', cluster: listed.cluster.name})),
      );
      expect(options.some(each => each.kind === 'modal')).toBe(settings.modal.token_present);
      expect(nothingSetUp({kind: 'offered', value: settings})).toBe(options.length === 1);
    }
  });

  test('a machine whose helper predates Settings, or not read yet, offers only itself', () => {
    expect(measureOptions(null)).toEqual([SAME_MACHINE]);
    expect(measureOptions({kind: 'unsupported'})).toEqual([SAME_MACHINE]);
    expect(nothingSetUp(null)).toBe(false);
    expect(nothingSetUp({kind: 'unsupported'})).toBe(true);
  });

  test('a choice the machine does not offer is put back to the same machine; an offered one stays', () => {
    for (const seed of SEEDS) {
      const random = new Seeded(seed);
      const compute: Compute = random.bool()
        ? {kind: 'offered', value: arbitrary(random)}
        : {kind: 'unsupported'};
      const chosen = random.pick<MeasureTarget>([
        SAME_MACHINE,
        {kind: 'modal'},
        {kind: 'slurm', cluster: random.pick(NAMES)},
      ]);
      const form = formMeasuring(chosen);
      const after = formWithMeasure(form, compute);
      const offered = measureOptions(compute).some(each => sameTarget(each, chosen));
      expect(after.measure).toEqual(offered ? chosen : SAME_MACHINE);
      expect(formWithMeasure(after, compute)).toBe(after);
      // Nothing is judged while the machine's data is not known.
      expect(formWithMeasure(form, null)).toBe(form);
    }
  });

  test('the target travels with the inputs: the checks are for exactly it', () => {
    const modal = runInputs(formMeasuring({kind: 'modal'}));
    const slurm = runInputs(formMeasuring({kind: 'slurm', cluster: 'lab-h100'}));
    expect(modal?.measure_on).toEqual({kind: 'modal'});
    expect(slurm?.measure_on).toEqual({kind: 'slurm', cluster: 'lab-h100'});
    expect(JSON.stringify(modal)).not.toBe(JSON.stringify(slurm));
  });

  test('a cluster is described by what its Settings entry says, and only by that', () => {
    const compute: Compute = {
      kind: 'offered',
      value: {
        config_path: '/c',
        clusters: [
          {
            cluster: {
              name: 'lab-h100',
              reach: {kind: 'ssh', host: 'lab-login'},
              workspace: '/w',
              partition: 'gpu',
              hardware: {accelerator: 'cuda', gpu_models: ['nvidia_h100'], gpus_per_node: 8},
            },
            source: 'user',
          },
        ],
        modal: {token_present: true},
      } as unknown as ComputeSettings,
    };
    const facts = (target: MeasureTarget) => targetFacts(compute, target).map(each => each.key);
    expect(facts({kind: 'slurm', cluster: 'lab-h100'})).toEqual(['Reached', 'Partition', 'GPUs']);
    expect(facts({kind: 'slurm', cluster: 'gone'})).toEqual([]);
    expect(facts({kind: 'modal'})).toEqual(['Token']);
    expect(facts(SAME_MACHINE)).toEqual([]);
    expect(targetFacts(null, {kind: 'modal'})).toEqual([]);
  });
});
