/** The New run form's numbered cards: the machine, the project and task, the agent, the budget. */

import {type CompletionAction, type CompletionState, suggestions} from '@vibesys/hosts';
import {
  Banner,
  Button,
  Card,
  Field,
  Grid,
  KeyValue,
  PathInput,
  Row,
  SegmentedControl,
  Select,
  Stack,
  Text,
  TextInput,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  advancedProblem,
  type Budget,
  type Effort,
  missingFeatures,
  type NewRunContext,
  type NewRunEvent,
  type ProjectChoice,
  ROLES,
  vettedModels,
} from '../../state/index.js';
import {type MachineOption, machineFacts, machineLabel, recentProjects} from './view.js';

type Dispatch = (event: NewRunEvent) => void;

interface CardProps {
  readonly context: NewRunContext;
  readonly dispatch: Dispatch;
}

/** 01: where the run runs, and what that machine offers. */
export function MachineCard(
  props: CardProps & {
    readonly machines: readonly MachineOption[];
    readonly chooseCheckout: () => void;
  },
): JSX.Element {
  const {context, dispatch} = props;
  const {machine} = context.form;
  const facts = context.facts?.for === machine ? context.facts.value : null;
  return (
    <Card section="01 · Machine">
      <Field label="Run on">
        <Select
          value={machine}
          options={
            props.machines.some(option => option.value === machine)
              ? props.machines
              : [...props.machines, {value: machine, label: machineLabel(machine)}]
          }
          change={next => dispatch({type: 'machine', machine: next})}
        />
      </Field>
      {facts === null ? (
        <Text variant="small" tone="muted">
          Asking {machineLabel(machine)} what it offers.
        </Text>
      ) : facts.kind === 'unsupported' ? (
        <Text variant="small" tone="muted">
          This machine's VibeSys checkout does not describe the machine; update it to see more.
        </Text>
      ) : facts.value.checkout === null ? (
        <Banner
          tone="warn"
          title={`No VibeSys checkout found on ${machineLabel(machine)}.`}
          actions={<Button text="Choose it in Machines" press={props.chooseCheckout} />}
        />
      ) : (
        <KeyValue items={machineFacts(facts.value)} />
      )}
    </Card>
  );
}

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
  return (
    <Field label="Project">
      <Select
        mono
        value={value}
        options={[
          ...(value === NONE ? [{value: NONE, label: 'Choose a project'}] : []),
          ...projects.map(path => ({value: path, label: path})),
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
  const choose = (example: string, destination: string): void =>
    dispatch({type: 'project', project: {source: 'example', example, destination}});
  return (
    <Grid min="field" gap="space-3">
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
            if (name !== NONE) choose(name, `~/vibesys-projects/${name}`);
          }}
        />
      </Field>
      {chosen === null ? null : (
        <Field label="Create it at" help="A new Git repository the run works in.">
          <TextInput
            mono
            value={chosen.destination}
            change={destination => choose(chosen.example, destination)}
          />
        </Field>
      )}
    </Grid>
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
  return (
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
  );
}

/** 02: the project (a recent one, a folder, or an example) and its task. */
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
    <Card section="02 · Project and task">
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

const EFFORTS: readonly {readonly value: Effort; readonly label: string}[] = [
  {value: 'default', label: 'Default'},
  {value: 'low', label: 'Low'},
  {value: 'medium', label: 'Medium'},
  {value: 'high', label: 'High'},
];

function RoleRows(props: CardProps & {readonly models: readonly string[]}): JSX.Element | null {
  const {context, dispatch, models} = props;
  const agent = context.form.agent;
  if (agent === null) return null;
  return (
    <Stack gap="space-3">
      {ROLES.map(({role, label, does}) => {
        const choice = agent.roles.find(each => each.role === role);
        if (choice === undefined) return null;
        return (
          <Grid key={role} min="field" gap="space-3">
            <Stack gap="space-1">
              <Text variant="body-strong">{label}</Text>
              <Text variant="small" tone="muted">
                {does}
              </Text>
            </Stack>
            <Field label={`${label} model`}>
              <Select
                mono
                value={choice.model ?? NONE}
                options={[
                  {value: NONE, label: 'Same as run'},
                  ...models.map(m => ({value: m, label: m})),
                ]}
                change={model =>
                  dispatch({type: 'role', role: {...choice, model: model === NONE ? null : model}})
                }
              />
            </Field>
            <Field label={`${label} reasoning effort`}>
              <Select
                value={choice.effort}
                options={EFFORTS}
                change={effort => dispatch({type: 'role', role: {...choice, effort}})}
              />
            </Field>
          </Grid>
        );
      })}
    </Stack>
  );
}

function rolesLabel(context: NewRunContext): string {
  const changed = (context.form.agent?.roles ?? []).filter(
    role => role.model !== null || role.effort !== 'default',
  ).length;
  return changed === 0 ? 'Roles: every role as the run' : `Roles: ${changed} changed`;
}

/** 03: the harness and vetted model every role runs, and each role's model and effort. */
export function AgentCard(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const [rolesShown, setRolesShown] = useState(false);
  const offers = context.harnesses?.for === context.form.machine ? context.harnesses.list : null;
  const agent = context.form.agent;
  if (offers === null) {
    return (
      <Card section="03 · Agent">
        <Text tone="muted">Asking the machine which agents it runs.</Text>
      </Card>
    );
  }
  if (agent === null) {
    return (
      <Card section="03 · Agent">
        <Text tone="warn">This machine offers no vetted agent harness.</Text>
      </Card>
    );
  }
  const providers = [...new Set(offers.map(offer => offer.provider))];
  const models = vettedModels(offers, agent.provider);
  return (
    <Card section="03 · Agent">
      <Grid min="field" gap="space-3">
        <Field label="Harness">
          <Select
            value={agent.provider}
            options={providers.map(provider => ({value: provider, label: provider}))}
            change={provider => dispatch({type: 'provider', provider})}
          />
        </Field>
        <Field label="Model" help="Only models VibeSys has tested with this harness are listed.">
          <Select
            mono
            value={agent.model}
            options={models.map(model => ({value: model, label: model}))}
            change={model => dispatch({type: 'model', model})}
          />
        </Field>
      </Grid>
      <Row>
        <Button
          variant="quiet"
          icon={rolesShown ? 'chevron-down' : 'chevron-right'}
          text={rolesLabel(context)}
          expanded={rolesShown}
          press={() => setRolesShown(!rolesShown)}
        />
      </Row>
      {rolesShown ? <RoleRows {...props} models={models} /> : null}
    </Card>
  );
}

const ROUNDS = ['configured', '1', '2', '3', '5', '10'] as const;
const TIME_LIMITS = [
  {value: 'none', label: 'None', seconds: null},
  {value: '3600', label: '1 hour', seconds: 3600},
  {value: '28800', label: '8 hours', seconds: 28800},
  {value: '86400', label: '24 hours', seconds: 86400},
] as const;

const RESULTS: readonly {readonly value: Budget['results']; readonly label: string}[] = [
  {value: 'keep', label: 'Keep on the machine'},
  {value: 'publish_github', label: 'Also publish to GitHub'},
];

/** `options`, plus the form's own `value` when none of them is it (a run started with another one). */
function withCurrent(
  options: readonly {readonly value: string; readonly label: string}[],
  value: string,
  label: string,
): {readonly value: string; readonly label: string}[] {
  return options.some(option => option.value === value)
    ? [...options]
    : [...options, {value, label}];
}

function BudgetFields(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  const {budget} = context.form;
  const change = (next: Partial<Budget>): void =>
    dispatch({type: 'budget', budget: {...budget, ...next}});
  return (
    <Grid min="field" gap="space-3">
      <Field label="Rounds" help="Searches without rounds, such as evolve, take none.">
        <Select
          value={budget.rounds === null ? 'configured' : String(budget.rounds)}
          options={withCurrent(
            ROUNDS.map(value => ({
              value,
              label: value === 'configured' ? 'As configured' : value,
            })),
            budget.rounds === null ? 'configured' : String(budget.rounds),
            String(budget.rounds),
          )}
          change={value => change({rounds: value === 'configured' ? null : Number(value)})}
        />
      </Field>
      <Field label="Time limit">
        <Select
          value={budget.timeLimitS === null ? 'none' : String(budget.timeLimitS)}
          options={withCurrent(
            TIME_LIMITS.map(({value, label}) => ({value, label})),
            budget.timeLimitS === null ? 'none' : String(budget.timeLimitS),
            `${budget.timeLimitS} seconds`,
          )}
          change={value => change({timeLimitS: value === 'none' ? null : Number(value)})}
        />
      </Field>
    </Grid>
  );
}

/** 04: rounds, time limit, where results go, and extra arguments. */
export function BudgetCard(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  // Opened by the user, else shown whenever it holds arguments (a run started with some).
  const [advancedChoice, setAdvancedChoice] = useState<boolean | null>(null);
  const advancedShown = advancedChoice ?? context.form.advanced !== '';
  const problem = advancedProblem(context.form.advanced);
  return (
    <Card section="04 · Budget and results">
      <BudgetFields {...props} />
      <Text variant="body-strong">Results</Text>
      <SegmentedControl
        label="Results"
        options={RESULTS}
        value={context.form.budget.results}
        change={results => dispatch({type: 'budget', budget: {...context.form.budget, results}})}
      />
      <Row>
        <Button
          variant="quiet"
          icon={advancedShown ? 'chevron-down' : 'chevron-right'}
          text="Advanced"
          expanded={advancedShown}
          press={() => setAdvancedChoice(!advancedShown)}
        />
      </Row>
      {advancedShown ? (
        <Field
          label="Extra arguments"
          help="Passed to vibesys run as typed; options set above may not be repeated."
          {...(problem === null ? {} : {error: problem})}
        >
          <TextInput
            mono
            value={context.form.advanced}
            change={advanced => {
              setAdvancedChoice(true);
              dispatch({type: 'advanced', advanced});
            }}
            placeholder="--max-in-flight 3"
          />
        </Field>
      ) : null}
    </Card>
  );
}
