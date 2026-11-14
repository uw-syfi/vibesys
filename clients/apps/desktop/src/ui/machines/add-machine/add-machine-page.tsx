import {
  Button,
  Card,
  type Crumb,
  Field,
  Grid,
  Page,
  PathInput,
  Row,
  SegmentedControl,
  Stack,
  Text,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {useState} from 'react';
import {
  emptyMachineDraft,
  type MachineConnectionKind,
  type MachineDraft,
  type MachineDraftResult,
  machineDraftResult,
} from '../../../state/index.js';
import type {AddMachineController} from './machine-controls.js';

const METHODS: readonly {readonly value: MachineConnectionKind; readonly label: string}[] = [
  {value: 'ssh-config', label: 'SSH config'},
  {value: 'direct', label: 'Connection details'},
  {value: 'command', label: 'Paste SSH command'},
];

const changed = <K extends keyof MachineDraft>(
  draft: MachineDraft,
  key: K,
  value: MachineDraft[K],
): MachineDraft => ({...draft, [key]: value});

function trail(
  origin: 'machines' | 'new-run',
  close: () => void,
  openRuns: () => void,
): readonly Crumb[] {
  return origin === 'new-run'
    ? [
        {id: 'runs', label: 'Runs', press: openRuns},
        {id: 'new-run', label: 'New run', press: close},
        {id: 'add-machine', label: 'Add machine or server'},
      ]
    : [
        {id: 'machines', label: 'Machines', press: close},
        {id: 'add-machine', label: 'Add machine or server'},
      ];
}

type SetDraft = <K extends keyof MachineDraft>(key: K, value: MachineDraft[K]) => void;

interface ConnectionFieldProps {
  readonly draft: MachineDraft;
  readonly controls: AddMachineController;
  readonly error: DraftFieldError | null;
  readonly set: SetDraft;
  readonly submit: () => void;
}

interface DraftFieldError {
  readonly field: keyof MachineDraft;
  readonly message: string;
}

function draftFieldError(
  draft: MachineDraft,
  result: MachineDraftResult,
  attempted: boolean,
): DraftFieldError | null {
  if (result.ready) return null;
  return attempted || String(draft[result.field]).trim() !== ''
    ? {field: result.field, message: result.reason}
    : null;
}

function connectionField(kind: MachineConnectionKind): keyof MachineDraft {
  if (kind === 'ssh-config') return 'alias';
  return kind === 'command' ? 'command' : 'host';
}

function connectionError(
  kind: MachineConnectionKind,
  local: DraftFieldError | null,
  submitted: string | null,
): DraftFieldError | null {
  if (submitted !== null) return {field: connectionField(kind), message: submitted};
  return local?.field === 'repositoriesDirectory' ? null : local;
}

function repositoriesError(
  local: DraftFieldError | null,
  saved: string | null,
): DraftFieldError | null {
  if (saved !== null) return {field: 'repositoriesDirectory', message: saved};
  return local?.field === 'repositoriesDirectory' ? local : null;
}

const errorAt = (
  error: DraftFieldError | null,
  field: keyof MachineDraft,
): {readonly error?: string} => (error?.field === field ? {error: error.message} : {});

function SshConfigFields(props: ConnectionFieldProps): JSX.Element {
  return (
    <Field
      label="SSH host or alias *"
      help="Uses Host, User, Port, ProxyJump and IdentityFile from ~/.ssh/config."
      {...errorAt(props.error, 'alias')}
    >
      <PathInput
        takeFocus
        value={props.draft.alias}
        change={alias => {
          props.set('alias', alias);
          props.controls.type(alias);
        }}
        completions={props.controls.state.matches.map(value => ({value}))}
        pick={completion => props.set('alias', completion.value)}
        submit={props.submit}
        placeholder="gpu-a"
      />
    </Field>
  );
}

function DirectFields(props: ConnectionFieldProps): JSX.Element {
  const {draft, set, submit} = props;
  return (
    <Stack gap="space-4">
      <Grid min="field">
        <Field label="Host name or address *" {...errorAt(props.error, 'host')}>
          <TextInput
            value={draft.host}
            change={host => set('host', host)}
            placeholder="host-a.example.com"
            mono
            submit={submit}
          />
        </Field>
        <Field
          label="Username"
          help="Optional; your SSH config default is used otherwise."
          {...errorAt(props.error, 'user')}
        >
          <TextInput
            value={draft.user}
            change={user => set('user', user)}
            placeholder="operator"
            mono
            submit={submit}
          />
        </Field>
        <Field
          label="Port"
          help="Optional; defaults to SSH configuration."
          {...errorAt(props.error, 'port')}
        >
          <TextInput
            value={draft.port}
            change={port => set('port', port)}
            placeholder="22"
            mono
            submit={submit}
          />
        </Field>
        <Field
          label="Jump host"
          help="Optional ProxyJump host or comma-separated chain."
          {...errorAt(props.error, 'jump')}
        >
          <TextInput
            value={draft.jump}
            change={jump => set('jump', jump)}
            placeholder="jump-a"
            mono
            submit={submit}
          />
        </Field>
      </Grid>
      <Field
        label="Identity file"
        help="Optional path to a private key already on this computer."
        {...errorAt(props.error, 'identityFile')}
      >
        <TextInput
          value={draft.identityFile}
          change={identityFile => set('identityFile', identityFile)}
          placeholder="~/.ssh/id_ed25519"
          mono
          submit={submit}
        />
      </Field>
    </Stack>
  );
}

function CommandFields(props: ConnectionFieldProps): JSX.Element {
  return (
    <Field
      label="SSH command *"
      help="Accepted options: -p, -l, -J, -i and equivalent SSH -o keys. Remote commands and shell syntax are refused."
      {...errorAt(props.error, 'command')}
    >
      <TextInput
        value={props.draft.command}
        change={command => props.set('command', command)}
        placeholder="ssh -p 2222 operator@host-a"
        mono
        submit={props.submit}
      />
    </Field>
  );
}

function ConnectionFields(props: ConnectionFieldProps): JSX.Element {
  switch (props.draft.kind) {
    case 'ssh-config':
      return <SshConfigFields {...props} />;
    case 'direct':
      return <DirectFields {...props} />;
    case 'command':
      return <CommandFields {...props} />;
  }
}

function ConnectionCard(props: ConnectionFieldProps): JSX.Element {
  return (
    <Card>
      <Stack gap="space-5">
        <Stack gap="space-2">
          <Text as="h2" variant="heading">
            Connection
          </Text>
          <Text tone="muted">
            Required fields are marked *. VibeSys validates a closed, non-interactive SSH command
            before saving anything.
          </Text>
        </Stack>
        <SegmentedControl
          label="Connection method"
          value={props.draft.kind}
          options={METHODS}
          change={kind => {
            props.set('kind', kind);
          }}
        />
        <ConnectionFields {...props} />
      </Stack>
    </Card>
  );
}

function WorkspaceCard(props: {
  readonly draft: MachineDraft;
  readonly error: DraftFieldError | null;
  readonly set: SetDraft;
  readonly submit: () => void;
}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-4">
        <Stack gap="space-1">
          <Text as="h2" variant="heading">
            Workspace
          </Text>
          <Text tone="muted">Optional defaults for repositories created on this machine.</Text>
        </Stack>
        <Field
          label="Repositories folder"
          help="Leave empty to use the machine's configured default."
          {...errorAt(props.error, 'repositoriesDirectory')}
        >
          <TextInput
            value={props.draft.repositoriesDirectory}
            change={folder => props.set('repositoriesDirectory', folder)}
            placeholder="~/vibesys-repositories"
            mono
            submit={props.submit}
          />
        </Field>
      </Stack>
    </Card>
  );
}

function ConnectionCheck(props: {readonly command: string | null}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-3">
        <Text as="h2" variant="heading">
          Connection check
        </Text>
        <Text tone="muted">
          VibeSys connects non-interactively. Test the command in your system terminal first if the
          host needs a password, key approval, or one-time host verification.
        </Text>
        <Text variant="section">Command preview</Text>
        <Text variant="code">{props.command ?? 'Complete the required connection fields.'}</Text>
      </Stack>
    </Card>
  );
}

interface AddMachinePageProps {
  readonly origin: 'machines' | 'new-run';
  readonly controls: AddMachineController;
  readonly close: () => void;
  readonly openRuns: () => void;
  readonly saveRepositories?: (
    host: string,
    folder: string,
    done: (error: string | null) => void,
  ) => void;
}

/** A dedicated structured SSH form. Authentication remains with SSH config or the SSH agent. */
export function AddMachinePage(props: AddMachinePageProps): JSX.Element {
  const [draft, setDraft] = useState<MachineDraft>(emptyMachineDraft);
  const [attempted, setAttempted] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);
  const result = machineDraftResult(draft);
  const connection = machineDraftResult({...draft, repositoriesDirectory: ''});
  const localError = draftFieldError(draft, result, attempted);
  const set = <K extends keyof MachineDraft>(key: K, value: MachineDraft[K]): void => {
    setDraft(current => changed(current, key, value));
    setAttempted(false);
    setSaveError(null);
    if (!props.controls.state.busy) props.controls.reset();
  };
  const done = (host: string): void => {
    if (!result.ready || result.repositoriesDirectory === '') {
      props.close();
      return;
    }
    if (props.saveRepositories === undefined) {
      setSaveError('This window cannot save a repositories folder.');
      return;
    }
    props.saveRepositories(host, result.repositoriesDirectory, error => {
      if (error === null) props.close();
      else setSaveError(error);
    });
  };
  const submit = (): void => {
    setAttempted(true);
    if (!result.ready || props.controls.state.busy) return;
    props.controls.submit(result.input, done);
  };
  const primaryError = connectionError(draft.kind, localError, props.controls.state.error);
  const workspaceError = repositoriesError(localError, saveError);
  return (
    <Page
      width="content"
      state="ready"
      title="Add machine or server"
      trail={trail(props.origin, props.close, props.openRuns)}
      meta={
        <Text tone="muted">
          Connect with your SSH config or explicit connection details. Authentication stays with
          SSH.
        </Text>
      }
    >
      <Stack gap="space-5">
        <ConnectionCard
          draft={draft}
          controls={props.controls}
          error={primaryError}
          set={set}
          submit={submit}
        />
        <WorkspaceCard draft={draft} error={workspaceError} set={set} submit={submit} />
        <ConnectionCheck command={connection.ready ? connection.input : null} />
        <Row gap="space-3">
          <Button
            variant="primary"
            text={props.controls.state.busy ? 'Adding machine…' : 'Add machine'}
            press={submit}
            {...(!result.ready || props.controls.state.busy
              ? {
                  disabledReason: result.ready ? 'Adding machine.' : result.reason,
                  quietReason: true,
                }
              : {})}
          />
          <Button variant="quiet" text="Cancel" press={props.close} />
        </Row>
      </Stack>
    </Page>
  );
}
