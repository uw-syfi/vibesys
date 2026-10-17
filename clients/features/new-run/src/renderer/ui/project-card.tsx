/** The Project card: a recent project, a folder or an example, and the task to run in it. */

import {type CompletionAction, type CompletionState, suggestions} from '@vibesys/hosts';
import {
  Button,
  Card,
  Field,
  PathInput,
  Row,
  SegmentedControl,
  Select,
  Stack,
  Text,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {missingFeatures, type ProjectChoice} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {backendLabel, backendOf, machineLabel, projectLabels, recentProjects} from './view.js';

type Source = ProjectChoice['source'];

const SOURCES: readonly {readonly value: Source; readonly label: string}[] = [
  {value: 'recent', label: 'Recent'},
  {value: 'folder', label: 'Open a folder'},
  {value: 'example', label: 'From an example'},
];

const NONE = '';

function RecentProject(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const projects = recentProjects(context.recent, context.form.machine);
  if (projects.length === 0) {
    return (
      <Text variant="small" tone="muted">
        No runs opened on this machine yet. Open a folder or start from an example.
      </Text>
    );
  }
  const project = context.form.project;
  const value = project?.source === 'recent' ? project.path : NONE;
  const labels = projectLabels(projects);
  return (
    <Field label="Project">
      <Select
        value={value}
        options={[
          ...(value === NONE ? [{value: NONE, label: 'Choose a project'}] : []),
          ...projects.map(path => ({value: path, label: labels.get(path) ?? path})),
        ]}
        change={path => {
          if (path !== NONE) dispatch({type: 'project', project: {source: 'recent', path}});
        }}
      />
    </Field>
  );
}

/** The folder field's path completion: the hosts' completion reducer's state and its actions. */
export interface FolderCompletion {
  readonly state: CompletionState;
  readonly dispatch: (action: CompletionAction) => void;
}

function FolderProject(props: CardProps & {readonly completion: FolderCompletion}): JSX.Element {
  const {context, dispatch, completion} = props;
  const project = context.form.project;
  const [draft, setDraft] = useState(project?.source === 'folder' ? project.path : '');
  const type = (value: string): void => {
    setDraft(value);
    completion.dispatch({type: 'input', field: 'project', host: context.form.machine, value});
  };
  const completions = suggestions(completion.state, 'project').map(value => ({value}));
  const open = (): void => {
    // A completion ends in `/`; the project is the folder itself.
    const path = draft.trim().replace(/(.)\/+$/, '$1');
    if (path !== '') dispatch({type: 'project', project: {source: 'folder', path}});
  };
  return (
    <Stack gap="space-2">
      <Field label={`Folder on ${machineLabel(context.form.machine)}`}>
        <PathInput value={draft} change={type} completions={completions} placeholder="~/project" />
      </Field>
      <Row>
        <Button text="Open" press={open} />
      </Row>
    </Stack>
  );
}

function ExampleProject(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const offered = context.examples?.for === context.form.machine ? context.examples.value : null;
  if (offered === null) return <Text tone="muted">Listing the examples.</Text>;
  if (offered.kind === 'unsupported' || offered.value.examples.length === 0) {
    return (
      <Text variant="small" tone="muted">
        This machine's VibeSys checkout offers no examples.
      </Text>
    );
  }
  const project = context.form.project;
  const chosen = project?.source === 'example' ? project : null;
  return (
    <Field label="Example">
      <Select
        value={chosen?.example ?? NONE}
        options={[
          ...(chosen === null ? [{value: NONE, label: 'Choose an example'}] : []),
          ...offered.value.examples.map(example => ({
            value: example.name,
            label: `${example.name} (${example.category})`,
          })),
        ]}
        change={name => {
          if (name !== NONE) {
            // Where the copy goes is under More options; it starts at the usual place.
            dispatch({
              type: 'project',
              project: {
                source: 'example',
                example: name,
                destination: `~/vibesys-projects/${name}`,
              },
            });
          }
        }}
      />
    </Field>
  );
}

function TaskField(props: CardProps): JSX.Element | null {
  const {context, dispatch} = props;
  const {project, task} = context.form;
  if (project === null || project.source === 'example') return null;
  const tasks = context.tasks?.for === project.path ? context.tasks.list : null;
  if (tasks === null) return <Text tone="muted">Reading the project's tasks.</Text>;
  if (tasks.length === 0) {
    return <Text tone="warn">This project defines no tasks.</Text>;
  }
  const backend = backendOf(context);
  return (
    <Stack gap="space-2">
      <Field label="Task">
        <Select
          mono
          value={task ?? NONE}
          options={[
            ...(task === null ? [{value: NONE, label: 'Choose a task'}] : []),
            ...tasks.map(name => ({value: name, label: name})),
          ]}
          change={name => {
            if (name !== NONE) dispatch({type: 'task', task: name});
          }}
        />
      </Field>
      {/* The task decides its backend: shown here, never a choice. */}
      {backend === null ? null : (
        <Text variant="small" tone="muted">
          Runs on {backendLabel(backend)}, as the task declares.
        </Text>
      )}
    </Stack>
  );
}

/** The project (a recent one, a folder, or an example) and its task. */
export function ProjectCard(
  props: CardProps & {readonly completion: FolderCompletion},
): JSX.Element {
  const [source, setSource] = useState<Source>(props.context.form.project?.source ?? 'recent');
  const {helper, form} = props.context;
  // A helper without `examples` cannot make one: the choice is not offered.
  const sources = missingFeatures(helper, form.machine).includes('examples')
    ? SOURCES.filter(option => option.value !== 'example')
    : SOURCES;
  return (
    <Card section="Project and task">
      <SegmentedControl
        label="Project source"
        options={sources}
        value={source}
        change={setSource}
      />
      {source === 'recent' ? <RecentProject {...props} /> : null}
      {source === 'folder' ? <FolderProject {...props} /> : null}
      {source === 'example' ? <ExampleProject {...props} /> : null}
      <TaskField {...props} />
    </Card>
  );
}
