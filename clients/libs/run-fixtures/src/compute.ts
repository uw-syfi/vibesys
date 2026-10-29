/**
 * What a helper answers the machine compute messages with (feature `machine_compute`) and the
 * `measurement_target` checks a preflight returns for a request's `measure_on` (feature
 * `measurement_target`): fixed copies of every shape the contract names, so a screen that
 * lists clusters, tests a connection and says whether a place fits the task can be built and
 * checked without a helper. `compute.test.ts` holds each valid against the helper contract.
 */
import type {
  Check,
  ClusterHardware,
  ClusterTestFailed,
  ClusterTestPassed,
  ComputeSettings,
  SlurmClusterSettings,
} from '@vibesys/contracts/host-helper';

const H100_NODES: ClusterHardware = {
  accelerator: 'cuda',
  gpu_models: ['nvidia_h100'],
  gpus_per_node: 8,
};
const MI300X_NODES: ClusterHardware = {
  accelerator: 'rocm',
  gpu_models: ['mi300x'],
  gpus_per_node: 8,
};

/** A cluster reached over an ssh host alias, with the hardware a connection test filled in. */
export const sshCluster: SlurmClusterSettings = {
  name: 'lab-h100',
  reach: {kind: 'ssh', host: 'lab-login'},
  workspace: '/scratch/vibesys',
  partition: 'gpu',
  nodes: 1,
  time_limit: '4:00:00',
  sbatch_options: ['--account=lab', '--gres=gpu:8'],
  hardware: H100_NODES,
};

/** A cluster the machine is a login node of, whose hardware was never stated. */
export const loginNodeCluster: SlurmClusterSettings = {
  name: 'campus',
  reach: {kind: 'login_node'},
  workspace: '/work/vibesys',
  sbatch_options: [],
  hardware: null,
};

/** A cluster of the operator file a terminal run reads: listed, never edited. */
export const operatorCluster: SlurmClusterSettings = {
  name: 'cluster-m',
  reach: {kind: 'ssh', host: 'amd-login'},
  workspace: '/work/vibesys-amd',
  partition: 'mi300x',
  sbatch_options: ['--partition=mi300x'],
  hardware: MI300X_NODES,
};

/** A machine with clusters in both files and a Modal token held in the environment. */
export const computeSettings: ComputeSettings = {
  config_path: '/home/me/.config/vibesys/compute.toml',
  clusters: [
    {cluster: sshCluster, source: 'vibesys'},
    {cluster: loginNodeCluster, source: 'vibesys'},
    {cluster: operatorCluster, source: 'slurm_toml'},
  ],
  modal: {token_present: true, source: 'environment'},
};

/** A machine where nothing is set up yet: no cluster, no Modal token. */
export const computeSettingsEmpty: ComputeSettings = {
  config_path: '/home/me/.config/vibesys/compute.toml',
  clusters: [],
  modal: {token_present: false},
};

/** The connection test of a cluster that answered: its partitions and the hardware read from `sinfo`. */
export const clusterTestPassed: ClusterTestPassed = {
  outcome: 'passed',
  hardware: H100_NODES,
  partitions: ['gpu', 'cpu'],
};

/** A cluster that passed with GPUs `sinfo` does not name a vendor for, so none is filled in. */
export const clusterTestPassedUntyped: ClusterTestPassed = {
  outcome: 'passed',
  hardware: {accelerator: null, gpu_models: [], gpus_per_node: 4},
  partitions: ['batch'],
};

/** One failed test per stage a connection test can fail at. */
export const clusterTestsFailed: readonly ClusterTestFailed[] = [
  {outcome: 'failed', stage: 'reach', detail: 'ssh: Could not resolve hostname lab-login'},
  {outcome: 'failed', stage: 'scheduler', detail: 'sinfo: error: Invalid partition'},
  {
    outcome: 'failed',
    stage: 'workspace',
    detail: 'the workspace /scratch/vibesys is missing or not writable',
  },
];

/** `measurement_target` checks: set up and fitting, not stated (a warning), not set up, a misfit. */
export const measurementChecks: Readonly<Record<string, Check>> = {
  fits: {
    id: 'measurement_target',
    state: 'ok',
    detail: "cluster 'lab-h100' offers the task's cuda backend",
  },
  sameMachine: {
    id: 'measurement_target',
    state: 'ok',
    detail: 'results are measured on this machine',
  },
  notStated: {
    id: 'measurement_target',
    state: 'warn',
    detail: "cluster 'campus' is set up; fit not checked: its hardware is not stated",
  },
  notConfigured: {
    id: 'measurement_target',
    state: 'fail',
    detail: "no Slurm cluster named 'ghost' is set up on this machine",
    fix: {kind: 'configure_compute', target: {kind: 'slurm', cluster: 'ghost'}},
    cause: {
      kind: 'measurement_target_not_configured',
      target: {kind: 'slurm', cluster: 'ghost'},
    },
  },
  modalWithoutToken: {
    id: 'measurement_target',
    state: 'fail',
    detail: 'Modal has no token on this machine',
    fix: {kind: 'configure_compute', target: {kind: 'modal'}},
    cause: {kind: 'measurement_target_not_configured', target: {kind: 'modal'}},
  },
  misfit: {
    id: 'measurement_target',
    state: 'fail',
    detail:
      "cluster 'cluster-m': the task targets the 'cuda' backend, which this cluster does not offer",
    cause: {
      kind: 'measurement_target_misfit',
      target: {kind: 'slurm', cluster: 'cluster-m'},
      backend: 'cuda',
      offered: 'rocm',
    },
  },
};
