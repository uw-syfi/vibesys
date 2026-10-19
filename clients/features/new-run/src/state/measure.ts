/**
 * Where results are measured, as New run offers it: this machine, each Slurm cluster set up on
 * it, and Modal when its token is there, read from the machine's Settings data (`ComputeSettings`).
 * A choice the machine no longer offers is put back to the same machine, so the form never starts
 * a run on a target the helper would refuse as not set up.
 */
import type {ComputeSettings, MeasureTarget, Offered} from '@vibesys/ipc';
import {type Form, SAME_MACHINE} from './form.js';

/** What a machine's Settings data says, or null while it is not known (or could not be read). */
export type Compute = Offered<ComputeSettings> | null;

/** The targets `compute` offers, this machine first, then its clusters by name, then Modal. */
export function measureOptions(compute: Compute): MeasureTarget[] {
  if (compute === null || compute.kind !== 'offered') return [SAME_MACHINE];
  const {clusters, modal} = compute.value;
  return [
    SAME_MACHINE,
    ...clusters.map((listed): MeasureTarget => ({kind: 'slurm', cluster: listed.cluster.name})),
    ...(modal.token_present ? [{kind: 'modal'} as const] : []),
  ];
}

/** The facts of the target `target` names on the machine `compute` describes, label and value. */
export function targetFacts(
  compute: Compute,
  target: MeasureTarget,
): readonly {readonly key: string; readonly value: string}[] {
  if (compute === null || compute.kind !== 'offered') return [];
  if (target.kind === 'modal') return [{key: 'Token', value: 'Found'}];
  if (target.kind !== 'slurm') return [];
  const cluster = compute.value.clusters.find(
    listed => listed.cluster.name === target.cluster,
  )?.cluster;
  if (cluster === undefined) return [];
  const {reach, partition, nodes, time_limit: limit, hardware} = cluster;
  return [
    {key: 'Reached', value: reach.kind === 'ssh' ? `SSH, ${reach.host}` : 'Login node'},
    ...(partition == null ? [] : [{key: 'Partition', value: partition}]),
    ...(nodes == null ? [] : [{key: 'Nodes', value: String(nodes)}]),
    ...(limit == null ? [] : [{key: 'Time limit', value: limit}]),
    ...(hardware == null
      ? []
      : [
          {
            key: 'GPUs',
            value: `${hardware.gpus_per_node}\u00d7 ${(hardware.gpu_models ?? []).join(', ')}`,
          },
        ]),
  ];
}

/** Whether no cluster or cloud is set up on the machine `compute` describes. */
export function nothingSetUp(compute: Compute): boolean {
  return compute !== null && measureOptions(compute).length === 1;
}

export function sameTarget(left: MeasureTarget, right: MeasureTarget): boolean {
  return (
    left.kind === right.kind &&
    (left.kind !== 'slurm' || left.cluster === (right as typeof left).cluster)
  );
}

/**
 * `form` measuring on what `compute` offers: a target it does not offer becomes the same machine.
 * While `compute` is not known, the choice stays (it is judged when it is).
 */
export function formWithMeasure(form: Form, compute: Compute): Form {
  if (compute === null) return form;
  const offered = measureOptions(compute).some(option => sameTarget(option, form.measure));
  return offered ? form : {...form, measure: SAME_MACHINE};
}
