/**
 * Clusters and clouds on one machine: its Slurm clusters (list, add after a passing connection
 * test, remove), whether Modal has a token there, and SkyPilot as a preview. It names the file the
 * machine keeps its settings in, as the machine's helper reports it.
 */

import type {ComputeSettings, SlurmClusterSettings} from '@vibesys/ipc';
import {
  Button,
  Card,
  Chip,
  EmptyState,
  Field,
  Grid,
  KeyValue,
  type KeyValueItem,
  Row,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type AcceleratorChoice,
  type ClusterFields,
  type ClusterForm,
  canSave,
  type FormField,
  hardwareModels,
  hardwareWords,
  type MachineSettingsAction,
  type MachineView,
  modalWords,
  type ReachKind,
  reachWords,
  stale,
  statedHardware,
} from '../../state/index.js';
import {Reading} from './reading.js';

type Dispatch = (action: MachineSettingsAction) => void;

const ACCELERATORS: readonly {value: AcceleratorChoice; label: string}[] = [
  {value: '', label: 'Not stated'},
  {value: 'cuda', label: 'NVIDIA (CUDA)'},
  {value: 'rocm', label: 'AMD (ROCm)'},
  {value: 'metal', label: 'Apple (Metal)'},
  {value: 'cpu', label: 'CPU only'},
];

const REACH: readonly {value: ReachKind; label: string}[] = [
  {value: 'ssh', label: 'SSH host alias'},
  {value: 'login_node', label: 'This machine is a login node'},
];

/** Why Save cluster cannot be pressed, or null when it can. */
function saveBlockedReason(form: ClusterForm): string | null {
  if (canSave(form)) return null;
  if (form.saving !== null) return 'Saving the cluster.';
  if (form.test.kind === 'untested' || form.test.kind === 'testing') {
    return 'Test the connection first.';
  }
  return stale(form)
    ? 'The settings changed since the test. Test the connection again.'
    : 'The test did not pass.';
}

interface FormProps {
  readonly host: string;
  readonly machine: string;
  readonly form: ClusterForm;
  readonly dispatch: Dispatch;
}

function IdentityFields(props: FormProps): JSX.Element {
  const {host, machine, form, dispatch} = props;
  const {fields} = form;
  const set = (key: keyof ClusterFields) => (value: string) =>
    dispatch({type: 'field', host, key, value});
  return (
    <>
      <Grid min="field">
        <Field label="Name" {...problemOf(form, 'name')}>
          <TextInput value={fields.name} placeholder="e.g. lab-h100" change={set('name')} />
        </Field>
        <Field label="Workspace on the cluster" {...problemOf(form, 'workspace')}>
          <TextInput
            mono
            value={fields.workspace}
            placeholder="e.g. /scratch/vibesys"
            change={set('workspace')}
          />
        </Field>
      </Grid>
      <Stack gap="space-2">
        <SegmentedControl
          label="How the cluster is reached"
          options={REACH}
          value={fields.reach}
          change={value => dispatch({type: 'field', host, key: 'reach', value})}
        />
        {fields.reach === 'ssh' ? (
          <Field
            label="SSH host alias"
            hint={`A Host entry in ${machine}'s ~/.ssh/config; its keys and options apply.`}
            {...problemOf(form, 'host')}
          >
            <TextInput mono value={fields.host} placeholder="e.g. lab-login" change={set('host')} />
          </Field>
        ) : null}
      </Stack>
    </>
  );
}

function SlurmFields(props: FormProps): JSX.Element {
  const {host, form, dispatch} = props;
  const {fields} = form;
  const set = (key: keyof ClusterFields) => (value: string) =>
    dispatch({type: 'field', host, key, value});
  return (
    <>
      <Grid min="field">
        <Field label="Partition" {...problemOf(form, 'partition')}>
          <TextInput value={fields.partition} placeholder="e.g. gpu" change={set('partition')} />
        </Field>
        <Field label="Nodes" {...problemOf(form, 'nodes')}>
          <TextInput value={fields.nodes} placeholder="e.g. 1" change={set('nodes')} />
        </Field>
        <Field label="Time limit" {...problemOf(form, 'timeLimit')}>
          <TextInput
            value={fields.timeLimit}
            placeholder="e.g. 4:00:00"
            change={set('timeLimit')}
          />
        </Field>
      </Grid>
      <Field
        label="Other sbatch options"
        hint="Separated by spaces, one option each, as sbatch takes them."
        {...problemOf(form, 'sbatch')}
      >
        <TextInput
          mono
          value={fields.sbatch}
          placeholder="e.g. --account=lab --gres=gpu:8"
          change={set('sbatch')}
        />
      </Field>
    </>
  );
}

function HardwareFields(props: FormProps): JSX.Element {
  const {host, form, dispatch} = props;
  const {fields} = form;
  const set = (key: keyof ClusterFields) => (value: string) =>
    dispatch({type: 'field', host, key, value});
  return (
    <>
      <Text variant="body-strong">Hardware the nodes offer</Text>
      <Grid min="field">
        <Field label="GPU vendor" {...problemOf(form, 'hardware')}>
          <Select
            value={fields.accelerator}
            options={ACCELERATORS}
            change={value => dispatch({type: 'field', host, key: 'accelerator', value})}
          />
        </Field>
        <Field label="GPU models" hint="Separated by commas.">
          <TextInput
            value={fields.gpuModels}
            placeholder="e.g. nvidia_h100"
            change={set('gpuModels')}
          />
        </Field>
        <Field label="GPUs per node">
          <TextInput value={fields.gpusPerNode} placeholder="e.g. 8" change={set('gpusPerNode')} />
        </Field>
      </Grid>
      <Text variant="small" tone="muted">
        Saved as: {hardwareWords(statedHardware(fields))}
      </Text>
    </>
  );
}

function FormActions(props: FormProps): JSX.Element {
  const {host, form, dispatch} = props;
  const testing = form.test.kind === 'testing';
  const saving = form.saving !== null;
  const blocked = saveBlockedReason(form);
  return (
    <Row gap="space-2" align="start" wrap>
      <Button
        text={testing ? 'Testing…' : 'Test connection'}
        hint="Must pass before the cluster is saved"
        {...(testing ? {disabledReason: 'Waiting for the cluster to answer.'} : {})}
        press={() => dispatch({type: 'test', host})}
      />
      <Button
        variant="primary"
        text={saving ? 'Saving…' : 'Save cluster'}
        {...(blocked === null ? {} : {disabledReason: blocked})}
        press={() => dispatch({type: 'save', host})}
      />
      <Button
        variant="quiet"
        text="Cancel"
        {...(saving ? {disabledReason: 'Saving the cluster.'} : {})}
        press={() => dispatch({type: 'close-form', host})}
      />
    </Row>
  );
}

/** The error under the control for `field`, when the form's problem is of it. */
function problemOf(form: ClusterForm, field: FormField): {error: string} | Record<string, never> {
  return form.problem?.field === field ? {error: form.problem.text} : {};
}

function ClusterFormCard(props: FormProps): JSX.Element {
  const {form} = props;
  return (
    <Card section="Add a Slurm cluster">
      <Stack>
        <IdentityFields {...props} />
        <SlurmFields {...props} />
        <HardwareFields {...props} />
        <TestResult form={form} outdated={stale(form)} />
        <FormActions {...props} />
      </Stack>
    </Card>
  );
}

function TestResult(props: {
  readonly form: ClusterForm;
  readonly outdated: boolean;
}): JSX.Element | null {
  const {test} = props.form;
  if (test.kind === 'untested' || test.kind === 'testing') return null;
  if (props.outdated) {
    return (
      <Chip label="Test again" tone="warn" dot hint="The settings changed since the last test." />
    );
  }
  if (test.kind === 'failed') return null;
  return (
    <Chip
      label="Connection works"
      tone="ok"
      dot
      hint={
        test.partitions.length === 0
          ? 'The hardware above was read from the scheduler where it could be.'
          : `Partitions: ${test.partitions.join(', ')}. The hardware above was read from the scheduler where it could be.`
      }
    />
  );
}

function clusterFacts(cluster: SlurmClusterSettings): KeyValueItem[] {
  return [
    {key: 'Reached', value: reachWords(cluster)},
    {key: 'Workspace', value: cluster.workspace, mono: true},
    ...(cluster.partition === null || cluster.partition === undefined
      ? []
      : [{key: 'Partition', value: cluster.partition}]),
    ...(cluster.nodes === null || cluster.nodes === undefined
      ? []
      : [{key: 'Nodes', value: String(cluster.nodes)}]),
    ...(cluster.time_limit === null || cluster.time_limit === undefined
      ? []
      : [{key: 'Time limit', value: cluster.time_limit}]),
    ...((cluster.sbatch_options ?? []).length === 0
      ? []
      : [{key: 'sbatch options', value: (cluster.sbatch_options ?? []).join(' '), mono: true}]),
    {
      key: 'Hardware',
      value: (
        <Row gap="space-2" wrap>
          <Text>{hardwareWords(cluster.hardware)}</Text>
          {hardwareModels(cluster.hardware).map(model => (
            <Text key={model} variant="code">
              {model}
            </Text>
          ))}
        </Row>
      ),
    },
  ];
}

function ClusterCard(props: {
  readonly host: string;
  readonly machine: string;
  readonly view: MachineView;
  readonly listed: ComputeSettings['clusters'][number];
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {host, machine, view, listed, dispatch} = props;
  const {cluster, source} = listed;
  const removing = view.removing?.name === cluster.name;
  const reason = removeBlocked(view, source);
  return (
    <Card>
      <Stack>
        <Row gap="space-3" wrap>
          <Stack grow>
            <Text as="h3" variant="item">
              {cluster.name}
            </Text>
          </Stack>
          {source === 'slurm_toml' ? <Chip label="From the operator file" /> : null}
        </Row>
        <KeyValue items={clusterFacts(cluster)} />
        <Row gap="space-2" align="start" wrap>
          <Button
            variant="quiet"
            text={removing ? 'Removing…' : 'Remove'}
            hint={reason ?? `Remove ${cluster.name} from ${machine}`}
            {...(reason === null ? {} : {disabledReason: reason, quietReason: true})}
            press={() => dispatch({type: 'remove', host, name: cluster.name})}
          />
        </Row>
      </Stack>
    </Card>
  );
}

/** Why a cluster of `source` cannot be removed now, or null when it can. */
function removeBlocked(view: MachineView, source: 'vibesys' | 'slurm_toml'): string | null {
  if (source === 'slurm_toml') {
    return 'This cluster comes from the Slurm operator file, which VibeSys does not edit.';
  }
  return view.removing === null ? null : 'Another removal is in progress.';
}

function Clusters(props: {
  readonly host: string;
  readonly machine: string;
  readonly view: MachineView;
  readonly settings: ComputeSettings;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {host, machine, view, settings, dispatch} = props;
  return (
    <Stack>
      <Row gap="space-3" align="end" wrap>
        <Stack gap="space-1" grow>
          <Text variant="body-strong">Slurm clusters</Text>
        </Stack>
        {view.form === null ? (
          <Button text="Add a cluster" press={() => dispatch({type: 'open-form', host})} />
        ) : null}
      </Row>
      {view.form === null ? null : (
        <ClusterFormCard host={host} machine={machine} form={view.form} dispatch={dispatch} />
      )}
      {settings.clusters.length === 0 && view.form === null ? (
        <Card>
          <EmptyState title="No Slurm cluster yet" />
        </Card>
      ) : null}
      {settings.clusters.map(listed => (
        <ClusterCard
          key={listed.cluster.name}
          host={host}
          machine={machine}
          view={view}
          listed={listed}
          dispatch={dispatch}
        />
      ))}
    </Stack>
  );
}

/** The Clusters and clouds section for `machine`, read from its helper. */
export function ClustersSection(props: {
  readonly host: string;
  readonly machine: string;
  readonly view: MachineView | undefined;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {host, machine, view, dispatch} = props;
  return (
    <Stack>
      <Stack gap="space-1">
        <Text as="h1" variant="title">
          Clusters and clouds
        </Text>
      </Stack>
      <Reading
        what="the clusters and clouds"
        machine={machine}
        reading={view?.compute}
        retry={() => dispatch({type: 'refresh', host, what: 'compute'})}
      >
        {settings =>
          view === undefined ? null : (
            <Stack>
              <Row gap="space-1" wrap>
                <Text variant="small" tone="muted" title={`Stored on ${machine}`}>
                  File
                </Text>
                <Text variant="code">{settings.config_path}</Text>
              </Row>
              <Clusters
                host={host}
                machine={machine}
                view={view}
                settings={settings}
                dispatch={dispatch}
              />
              <Card section="Modal">
                <Stack gap="space-2">
                  <Row gap="space-2" wrap>
                    <Chip
                      dot
                      label={modalWords(settings.modal).label}
                      tone={modalWords(settings.modal).tone}
                      hint={modalWords(settings.modal).detail}
                    />
                  </Row>
                </Stack>
              </Card>
              <Card section="SkyPilot">
                <Stack gap="space-2">
                  <Row gap="space-2" wrap>
                    <Chip label="Preview" />
                  </Row>
                  <Text tone="muted">Not available</Text>
                </Stack>
              </Card>
            </Stack>
          )
        }
      </Reading>
    </Stack>
  );
}
