import type {
  ListCatalogueResponse,
  ListEvalPlacesResponse,
  ModelCatalog,
} from '@vibesys/orchestrator-proto';
import {Button, Field, PathInput, Row, Select, Stack, TextInput} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  type CommandProgress,
  completionsOf,
  type FolderListing,
  listingKey,
  type PathCompletion,
  type ProvisionProgress,
  type Remote,
  type RepositoryChecked,
  type RepositoryVerdict,
  repositoryVerdict,
  splitPath,
} from '../../state/index.js';
import {CommandLine} from './command-line.js';
import {SetUpButton} from './setup/set-up-button.js';
import {
  checkDraft,
  DEFAULT_FANOUT,
  type DraftCheck,
  type DraftProblem,
  FANOUT_CHOICES,
  LEAD_CHOICES,
  LEADS,
  type LeadChoice,
  type ProblemField,
  type StartDraft,
  type StartRunRequest,
  TIME_LIMITS,
} from './start-draft.js';
import {type ModelChoices, modelChoices, NO_CATALOG, startModel} from './start-models.js';
import {harnessWord} from './words.js';

/** A host the run can start on: its key, and what it is called on screen. */
export interface StartHost {
  readonly key: string;
  readonly label: string;
  /** What the host said it runs agents on and allows, or null before it has said. */
  readonly models: ModelCatalog | null;
  /** The host's latest answer about a repository path, with the path it was about. */
  readonly repositoryChecked: RepositoryChecked | undefined;
  /** What the host listed of each folder asked for, by `listingKey`. */
  readonly directories: ReadonlyMap<string, FolderListing>;
  /** The host's catalogue of example tasks, with its verdict on each. */
  readonly catalogue: Remote<ListCatalogueResponse>;
  /** The host's eval places. */
  readonly evalPlaces: Remote<ListEvalPlacesResponse>;
  /** The Repositories folder kept for this machine, empty when none: where Copy to starts. */
  readonly repositoriesDirectory?: string;
  /** Where the repository being made from an example stands. */
  readonly provision: ProvisionProgress;
}

const timeWord = (minutes: number): string =>
  minutes < 60 ? `${minutes} min` : `${minutes / 60} h`;

/** The model control's options: the tool's default first where the host leaves it free, then the models. */
const modelOptions = (choices: ModelChoices) => [
  ...(choices.pinned ? [] : [{value: '', label: 'Default'}]),
  ...choices.models.map(id => ({value: id, label: id})),
];

/** The catalogue half of New run, in place of the repository and task fields. */
export interface StartExample {
  /** What the person picks the example, its place and its folder with. */
  readonly picker: JSX.Element;
  /** Why no example can be started yet, or null. */
  readonly blocked: string | null;
  /** Make the example's repository and start in it; the request names no repository yet. */
  readonly start: (host: string, request: StartRunRequest) => void;
}

/** A field's error as the Field takes it: the message, or nothing when the field shows none. */
type FieldError = {readonly error?: string};
type ErrorAt = (field: ProblemField) => FieldError;

/** How the line under Start names the field whose error is drawn under it. */
const FIELD_WORDS: Readonly<Record<ProblemField, string>> = {
  repository: 'repository',
  task: 'task',
  model: 'model',
  costLimit: 'cost limit',
};

/**
 * Whether a problem is drawn under its field. A model the host refuses and a cost that does not read
 * show at once; a required field left empty shows once it was typed in (or holds only spaces), so a
 * form just opened is not all errors. Before then, the line under Start says what is missing.
 */
function shownAtField(problem: DraftProblem, draft: StartDraft, typed: ReadonlySet<string>) {
  const {field} = problem;
  return field === 'model' || field === 'costLimit' || typed.has(field) || draft[field] !== '';
}

/** The problems drawn under their fields, and each field's error as its Field takes it. */
function fieldErrors(check: DraftCheck, draft: StartDraft, typed: ReadonlySet<string>) {
  const shown =
    check._tag === 'Ready'
      ? []
      : check.problems.filter(problem => shownAtField(problem, draft, typed));
  const errorAt: ErrorAt = field => {
    const message = shown.find(problem => problem.field === field)?.message;
    return message === undefined ? {} : {error: message};
  };
  return {shown, errorAt};
}

/** What the draft lacks, or null: no host, or the first problem, pointed to when drawn at its field. */
function startReason(
  host: StartHost | undefined,
  check: DraftCheck,
  shown: readonly DraftProblem[],
): string | null {
  if (host === undefined) return 'No host is connected.';
  if (check._tag === 'Ready') return null;
  const [first] = check.problems;
  return shown.includes(first) ? `Check the ${FIELD_WORDS[first.field]}.` : first.message;
}

/** The run's bounds: how long it may take and what it may cost, each none unless set. */
function LimitFields(props: {
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly errorAt: ErrorAt;
}): JSX.Element {
  const {draft, set} = props;
  return (
    <>
      <Field label="Time limit">
        <Select
          value={String(draft.timeLimit)}
          options={TIME_LIMITS.map(minutes => ({
            value: String(minutes),
            label: minutes === null ? 'None' : timeWord(minutes),
          }))}
          change={chosen =>
            set({timeLimit: TIME_LIMITS.find(minutes => String(minutes) === chosen) ?? null})
          }
        />
      </Field>
      <Field label="Cost limit ($)" {...props.errorAt('costLimit')}>
        <TextInput
          value={draft.costLimit}
          change={costLimit => set({costLimit})}
          placeholder="None"
          mono
        />
      </Field>
    </>
  );
}

/** The most task names the line under the repository field names before it counts the rest. */
const TASKS_SHOWN = 3;

/** The one line under the repository field when the host passed it: its tasks, or nothing. */
function tasksLine(verdict: RepositoryVerdict): string | undefined {
  if (verdict._tag !== 'Valid' || verdict.tasks.length === 0) return undefined;
  const more = verdict.tasks.length - TASKS_SHOWN;
  const shown = verdict.tasks.slice(0, TASKS_SHOWN).join(', ');
  return `Tasks: ${shown}${more > 0 ? ` +${more}` : ''}`;
}

/**
 * The repository path, completed from the host's folders as it is typed, with its problem under the
 * field: the draft's own (left empty once typed in) first, else the host's answer about the path (its
 * refusal, or the tasks it holds). None of the three ever meet, as an empty path has no verdict and a
 * refused path holds no tasks.
 */
function RepositoryField(props: {
  readonly value: string;
  readonly problem: FieldError;
  readonly verdict: RepositoryVerdict;
  readonly completions: readonly PathCompletion[];
  readonly midName: boolean;
  readonly change: (repository: string) => void;
  readonly enter: () => void;
}): JSX.Element {
  const {verdict} = props;
  const tasks = tasksLine(verdict);
  const refused = verdict._tag === 'Invalid' ? {error: verdict.reason} : {};
  return (
    <Field
      label="Repository"
      reserveLine
      {...(tasks === undefined ? {} : {help: tasks})}
      {...(props.problem.error === undefined ? refused : props.problem)}
    >
      <PathInput
        value={props.value}
        change={props.change}
        completions={props.completions.map(({value, detail}) => ({
          value,
          ...(detail === undefined ? {} : {detail}),
        }))}
        tabCompletes={props.midName}
        focus={props.enter}
        placeholder="/path/to/repository"
      />
    </Field>
  );
}

/** Why Start cannot be pressed yet and whether that is said aloud, or null: sending, what is missing, the host's word on the path. */
function startBlock(
  sending: boolean,
  missing: string | null,
  verdict: RepositoryVerdict,
): {readonly reason: string; readonly quiet: boolean} | null {
  if (sending) return {reason: 'Sending.', quiet: true};
  if (missing !== null) return {reason: missing, quiet: false};
  if (verdict._tag === 'Invalid') return {reason: 'Fix the repository.', quiet: true};
  return verdict._tag === 'Checking' ? {reason: 'Checking.', quiet: true} : null;
}

/** What the repository field offers for the path typed, and how it asks the host to list a folder. */
function pathCompletion(
  host: StartHost | undefined,
  repository: string,
  listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void,
): {
  readonly completions: readonly PathCompletion[];
  /** The path ends in the middle of a name, so Tab may finish it. */
  readonly midName: boolean;
  /** List the folder `repository` has reached on `onHost`; `fresh` reads it again. */
  readonly list: (onHost: string, repository: string, fresh: boolean) => void;
} {
  const split = splitPath(repository);
  return {
    completions:
      split === undefined
        ? []
        : completionsOf(split, host?.directories.get(listingKey(split.folder, split.hidden))),
    midName: split !== undefined && split.prefix !== '',
    list: (onHost, typed, fresh) => {
      const reached = splitPath(typed);
      if (reached !== undefined) listDirectory(onHost, reached.folder, reached.hidden, fresh);
    },
  };
}

/** How many agents at most, the lead's tool, and its model. */
function AgentFields(props: {
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly choices: ModelChoices;
  readonly model: string;
  readonly errorAt: ErrorAt;
}): JSX.Element {
  const {draft, set, choices, model} = props;
  return (
    <>
      <Field label="Fanout" help="The most agents the run may have at once.">
        <Select
          value={String(draft.fanout)}
          options={FANOUT_CHOICES.map(count => ({
            value: String(count),
            label: count === DEFAULT_FANOUT ? `${count} (default)` : String(count),
          }))}
          change={value => set({fanout: Number(value)})}
        />
      </Field>
      <Field
        label="Lead tool"
        help="The tool the lead agent runs on. It picks the tools of the rest."
      >
        <Select
          value={draft.lead}
          options={LEAD_CHOICES.map(choice => ({
            value: choice,
            label: harnessWord(LEADS[choice]),
          }))}
          change={(choice: LeadChoice) => set({lead: choice, model: ''})}
        />
      </Field>
      <Field label="Model" {...props.errorAt('model')}>
        <Select
          value={model}
          options={modelOptions(choices)}
          change={chosen => set({model: chosen})}
          mono
        />
      </Field>
    </>
  );
}

/** The person's own repository: its path (completed from the host's folders), its task, and its eval place. */
function RepositoryTarget(props: {
  readonly host: StartHost | undefined;
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly errorAt: ErrorAt;
  readonly verdict: RepositoryVerdict;
  readonly path: ReturnType<typeof pathCompletion>;
  readonly askAbout: (repository: string) => void;
  readonly placeField: JSX.Element | undefined;
}): JSX.Element {
  const {host, draft, set, path} = props;
  return (
    <>
      <RepositoryField
        value={draft.repository}
        problem={props.errorAt('repository')}
        verdict={props.verdict}
        completions={path.completions}
        midName={path.midName}
        change={repository => {
          set({repository});
          props.askAbout(repository);
          if (host !== undefined) path.list(host.key, repository, false);
        }}
        enter={() => {
          if (host !== undefined) path.list(host.key, draft.repository, true);
        }}
      />
      <Field
        label="Task"
        help="The task to optimize, as named in the repository."
        {...props.errorAt('task')}
      >
        <TextInput value={draft.task} change={task => set({task})} placeholder="task" mono />
      </Field>
      {props.placeField}
    </>
  );
}

/**
 * Start a run: the host, the repository and task, how many agents at most, the lead's tool, and
 * optional steering. The draft is the caller's, so it stays filled in after a press (a run that was
 * not accepted can be fixed and sent again) and after the form is put away; the progress of the last
 * press shows under the button.
 */
export function StartRunForm(props: {
  readonly hosts: readonly StartHost[];
  readonly draft: StartDraft;
  readonly setDraft: (draft: StartDraft) => void;
  /** The key of the host picked, or null before the person picks one (the first host then). */
  readonly chosen: string | null;
  readonly choose: (host: string) => void;
  /** What the last press of Start came to, or undefined before any. */
  readonly progress: CommandProgress | undefined;
  readonly start: (host: string, request: StartRunRequest) => void;
  /** The repository path in the field is now `repository` on `host`: ask the host about it. */
  readonly checkRepository: (host: string, repository: string) => void;
  /**
   * Ask `host` for the entries of `folder` (the host lists it once), hidden ones too when `hidden`;
   * with `fresh`, read again what it listed before.
   */
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
  /** Send the last Start again, under the id it already has. */
  readonly retry: () => void;
  /** Show the run a Start created. */
  readonly openRun: (host: string, runId: string) => void;
  /** Start from an example of the catalogue, not from a repository the person names. */
  readonly example?: StartExample;
  /** The control for the eval place of a run in the person's own repository. */
  readonly placeField?: JSX.Element;
  /** Begin a setup chat in the repository named, which makes its task; for a run in the person's own repository. */
  readonly setUp?: (host: string, request: StartRunRequest) => void;
}): JSX.Element {
  const {draft, example} = props;
  // The fields typed in since the form was opened, so an emptied one can say it needs filling.
  const [typed, setTyped] = useState<ReadonlySet<string>>(() => new Set());
  const host = props.hosts.find(candidate => candidate.key === props.chosen) ?? props.hosts[0];
  const set = (change: Partial<StartDraft>): void => {
    setTyped(before => new Set([...before, ...Object.keys(change)]));
    props.setDraft({...draft, ...change});
  };
  const choices = host === undefined ? NO_CATALOG : modelChoices(host.models, LEADS[draft.lead]);
  const model = startModel(draft.model, choices);
  const check = checkDraft(draft, choices, example === undefined ? 'repository' : 'example');
  const {shown, errorAt} = fieldErrors(check, draft, typed);
  const verdict =
    example === undefined
      ? repositoryVerdict(draft.repository, host?.repositoryChecked)
      : ({_tag: 'Empty'} as const);
  const missing = startReason(host, check, shown) ?? example?.blocked ?? null;
  const block = startBlock(props.progress?._tag === 'Sending', missing, verdict);
  const askAbout = (repository: string): void => {
    if (host !== undefined) props.checkRepository(host.key, repository);
  };
  const path = pathCompletion(host, draft.repository, props.listDirectory);
  return (
    <Stack gap="space-3">
      {props.hosts.length > 1 ? (
        <Field label="Host">
          <Select
            value={host?.key ?? ''}
            options={props.hosts.map(option => ({value: option.key, label: option.label}))}
            change={key => {
              props.choose(key);
              props.checkRepository(key, draft.repository);
              path.list(key, draft.repository, true);
            }}
            mono
          />
        </Field>
      ) : null}
      {example === undefined ? (
        <RepositoryTarget
          host={host}
          draft={draft}
          set={set}
          errorAt={errorAt}
          verdict={verdict}
          path={path}
          askAbout={askAbout}
          placeField={props.placeField}
        />
      ) : (
        example.picker
      )}
      <AgentFields draft={draft} set={set} choices={choices} model={model} errorAt={errorAt} />
      <LimitFields draft={draft} set={set} errorAt={errorAt} />
      <Field label="Steering (optional)" help="Anything the lead should know or prefer.">
        <TextInput
          value={draft.steering}
          change={steering => set({steering})}
          placeholder="For example: prefer changes that keep memory use flat"
          rows={3}
        />
      </Field>
      <Row>
        <Button
          text="Start run"
          label="Start run"
          variant="primary"
          press={() => {
            if (host === undefined || block !== null || check._tag !== 'Ready') return;
            (example?.start ?? props.start)(host.key, check.request);
          }}
          {...(block === null ? {} : {disabledReason: block.reason, quietReason: block.quiet})}
        />
        {example === undefined && host !== undefined && props.setUp !== undefined ? (
          <SetUpButton
            draft={draft}
            choices={choices}
            verdict={verdict}
            sending={props.progress?._tag === 'Sending'}
            setUp={request => props.setUp?.(host.key, request)}
          />
        ) : null}
      </Row>
      <CommandLine progress={props.progress} done="Start" retry={props.retry} />
      {props.progress?._tag === 'Accepted' &&
      props.progress.receipt.runId !== '' &&
      host !== undefined ? (
        <Button
          text="Open the run"
          variant="secondary"
          size="sm"
          press={() => {
            if (props.progress?._tag === 'Accepted') {
              props.openRun(host.key, props.progress.receipt.runId);
            }
          }}
        />
      ) : null}
    </Stack>
  );
}
