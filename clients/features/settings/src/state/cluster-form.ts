/**
 * The Add a cluster form: its fields as typed, the cluster they make, and what a connection test
 * said of exactly that cluster. A test counts only for the cluster it was run on: any change to a
 * field that is sent to the test (everything but the hardware, which the test fills in) makes the
 * form untested again, so a cluster is never saved on another one's test.
 */
import type {ClusterTest, SlurmClusterSettings} from '@vibesys/ipc';

/** The fields a problem is shown under. */
export type FormField =
  | 'name'
  | 'host'
  | 'workspace'
  | 'partition'
  | 'nodes'
  | 'timeLimit'
  | 'sbatch'
  | 'hardware';

export type ReachKind = 'ssh' | 'login_node';
export type AcceleratorChoice = '' | 'cpu' | 'cuda' | 'rocm' | 'metal';

/** The text of each field, as the user typed it. */
export interface ClusterFields {
  readonly name: string;
  readonly reach: ReachKind;
  readonly host: string;
  readonly workspace: string;
  readonly partition: string;
  readonly nodes: string;
  readonly timeLimit: string;
  /** Further sbatch options, separated by spaces. */
  readonly sbatch: string;
  readonly accelerator: AcceleratorChoice;
  /** GPU models, separated by commas. */
  readonly gpuModels: string;
  readonly gpusPerNode: string;
}

export const EMPTY_FIELDS: ClusterFields = {
  name: '',
  reach: 'ssh',
  host: '',
  workspace: '',
  partition: '',
  nodes: '',
  timeLimit: '',
  sbatch: '',
  accelerator: '',
  gpuModels: '',
  gpusPerNode: '',
};

/** What the form knows of its connection test. */
export type FormTest =
  | {readonly kind: 'untested'}
  | {readonly kind: 'testing'; readonly token: number; readonly sent: string}
  | {
      readonly kind: 'passed';
      readonly sent: string;
      readonly partitions: readonly string[];
    }
  | {
      readonly kind: 'failed';
      readonly sent: string;
      readonly stage: 'reach' | 'scheduler' | 'workspace';
      readonly detail: string;
    };

/** What is wrong with the form: one field, or the form as a whole when `field` is null. */
export interface FormProblem {
  readonly field: FormField | null;
  readonly text: string;
  /** The status word of a failure of the form as a whole (`field` null); its `text` is the detail. */
  readonly word?: string;
}

export interface ClusterForm {
  readonly fields: ClusterFields;
  /** Whether the user has typed in a hardware field, so a test no longer fills them. */
  readonly hardwareEdited: boolean;
  readonly test: FormTest;
  /** The save awaited, or null. */
  readonly saving: number | null;
  readonly problem: FormProblem | null;
}

export const NEW_FORM: ClusterForm = {
  fields: EMPTY_FIELDS,
  hardwareEdited: false,
  test: {kind: 'untested'},
  saving: null,
  problem: null,
};

const HARDWARE_FIELDS: readonly (keyof ClusterFields)[] = [
  'accelerator',
  'gpuModels',
  'gpusPerNode',
];

/** `fields` with `key` set; a hardware field also marks the hardware as the user's own. */
export function edited(form: ClusterForm, key: keyof ClusterFields, value: string): ClusterForm {
  return {
    ...form,
    fields: {...form.fields, [key]: value},
    hardwareEdited: form.hardwareEdited || HARDWARE_FIELDS.includes(key),
    problem: null,
  };
}

type Built =
  | {readonly ok: true; readonly cluster: SlurmClusterSettings}
  | {readonly ok: false; readonly problem: FormProblem};

function words(text: string, separator: RegExp): string[] {
  return text
    .split(separator)
    .map(part => part.trim())
    .filter(part => part !== '');
}

function count(
  text: string,
  what: string,
  field: FormField,
  atLeast: number,
): number | FormProblem {
  const value = Number(text.trim());
  if (!Number.isSafeInteger(value) || value < atLeast) {
    return {field, text: `${what} must be a whole number of ${atLeast} or more.`};
  }
  return value;
}

type Hardware = NonNullable<SlurmClusterSettings['hardware']>;

/** The hardware the form's fields state, or null when they state none or are not yet valid. */
export function statedHardware(fields: ClusterFields): Hardware | null {
  const stated = hardwareOf(fields);
  return stated === null || 'text' in stated ? null : stated;
}

function hardwareOf(fields: ClusterFields): Hardware | null | FormProblem {
  const models = words(fields.gpuModels, /,/);
  const gpus = fields.gpusPerNode.trim();
  if (fields.accelerator === '' && models.length === 0 && gpus === '') return null;
  const perNode = gpus === '' ? 0 : count(gpus, 'GPUs per node', 'hardware', 0);
  if (typeof perNode !== 'number') return perNode;
  if (fields.accelerator === 'cpu' && (models.length > 0 || perNode > 0)) {
    return {field: 'hardware', text: 'A CPU-only cluster offers no GPU models or GPU count.'};
  }
  return {
    accelerator: fields.accelerator === '' ? null : fields.accelerator,
    gpu_models: models,
    gpus_per_node: perNode,
  };
}

/** The cluster `fields` describe, or the first thing that is missing or not a number. */
export function buildCluster(fields: ClusterFields): Built {
  const missing = (field: FormField, text: string): Built => ({ok: false, problem: {field, text}});
  if (fields.name.trim() === '') return missing('name', 'A cluster needs a name.');
  if (fields.reach === 'ssh' && fields.host.trim() === '') {
    return missing('host', 'Name the SSH host alias that reaches the cluster.');
  }
  if (fields.workspace.trim() === '') {
    return missing('workspace', 'Name the directory on the cluster where runs stage their work.');
  }
  const nodes = fields.nodes.trim() === '' ? null : count(fields.nodes, 'Nodes', 'nodes', 1);
  if (nodes !== null && typeof nodes !== 'number') return {ok: false, problem: nodes};
  const hardware = hardwareOf(fields);
  if (hardware !== null && 'text' in hardware) return {ok: false, problem: hardware};
  const partition = fields.partition.trim();
  const timeLimit = fields.timeLimit.trim();
  return {
    ok: true,
    cluster: {
      name: fields.name.trim(),
      reach:
        fields.reach === 'ssh' ? {kind: 'ssh', host: fields.host.trim()} : {kind: 'login_node'},
      workspace: fields.workspace.trim(),
      ...(partition === '' ? {} : {partition}),
      ...(nodes === null ? {} : {nodes}),
      ...(timeLimit === '' ? {} : {time_limit: timeLimit}),
      sbatch_options: words(fields.sbatch, /\s+/),
      ...(hardware === null ? {} : {hardware}),
    },
  };
}

/** What a test is of: the cluster without its hardware, which the test itself fills in. */
export function sentOf(cluster: SlurmClusterSettings): string {
  const {hardware: _hardware, ...sent} = cluster;
  return JSON.stringify(sent);
}

/** The form after the test of the cluster `sent` answered `result`; the hardware is filled in. */
export function tested(form: ClusterForm, sent: string, result: ClusterTest): ClusterForm {
  if (result.outcome === 'failed') {
    return {
      ...form,
      test: {kind: 'failed', sent, stage: result.stage, detail: result.detail},
      problem: null,
    };
  }
  const hardware = result.hardware;
  const filled: ClusterFields = form.hardwareEdited
    ? form.fields
    : {
        ...form.fields,
        accelerator: hardware.accelerator ?? '',
        gpuModels: (hardware.gpu_models ?? []).join(', '),
        gpusPerNode: hardware.gpus_per_node === undefined ? '' : String(hardware.gpus_per_node),
      };
  return {
    ...form,
    fields: filled,
    test: {kind: 'passed', sent, partitions: result.partitions},
    problem: null,
  };
}

/** Whether the form may be saved: its cluster is the one that passed the test. */
export function canSave(form: ClusterForm): boolean {
  if (form.test.kind !== 'passed' || form.saving !== null) return false;
  const built = buildCluster(form.fields);
  return built.ok && sentOf(built.cluster) === form.test.sent;
}

/** Whether the shown test result is of an older cluster than the fields now describe. */
export function stale(form: ClusterForm): boolean {
  if (form.test.kind === 'untested') return false;
  const built = buildCluster(form.fields);
  return !built.ok || sentOf(built.cluster) !== form.test.sent;
}

const KEYS: readonly (readonly [RegExp, FormField])[] = [
  [/\bcluster\.name\b/, 'name'],
  [/\bcluster\.reach\.host\b/, 'host'],
  [/\bcluster\.workspace\b/, 'workspace'],
  [/\bcluster\.partition\b/, 'partition'],
  [/\bcluster\.nodes\b/, 'nodes'],
  [/\bcluster\.time_limit\b/, 'timeLimit'],
  [/\bcluster\.sbatch_options\b/, 'sbatch'],
  [/\bcluster\.hardware\b/, 'hardware'],
  [/already set up/, 'name'],
];

/**
 * `error` as a problem, under the field it names when it names one; otherwise it is the form's,
 * with `word` as its status.
 */
export function problemOf(error: string, word: string): FormProblem {
  const field = KEYS.find(([pattern]) => pattern.test(error))?.[1] ?? null;
  return field === null ? {field, text: error, word} : {field, text: error};
}
