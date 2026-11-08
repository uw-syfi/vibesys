import type {ModelCatalog} from '@vibesys/orchestrator-proto';
import {Button, Field, Row, Select, Stack, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CommandProgress,
  type RepositoryChecked,
  type RepositoryVerdict,
  repositoryVerdict,
} from '../../state/index.js';
import {CommandLine} from './command-line.js';
import {
  DEFAULT_FANOUT,
  FANOUT_CHOICES,
  LEAD_CHOICES,
  LEADS,
  type LeadChoice,
  missingFrom,
  type StartDraft,
  type StartRunRequest,
  startRunRequest,
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
}

const timeWord = (minutes: number): string =>
  minutes < 60 ? `${minutes} min` : `${minutes / 60} h`;

/** The model control's options: the tool's default first where the host leaves it free, then the models. */
const modelOptions = (choices: ModelChoices) => [
  ...(choices.pinned ? [] : [{value: '', label: 'Default'}]),
  ...choices.models.map(id => ({value: id, label: id})),
];

/** The run's bounds: how long it may take and what it may cost, each none unless set. */
function LimitFields(props: {
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
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
      <Field label="Cost limit ($)">
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

/** The repository path, with the host's refusal of it under the field. */
function RepositoryField(props: {
  readonly value: string;
  readonly verdict: RepositoryVerdict;
  readonly change: (repository: string) => void;
}): JSX.Element {
  const {verdict} = props;
  return (
    <Field
      label="Repository"
      help="The path of the repository on the host."
      {...(verdict._tag === 'Invalid' ? {error: verdict.reason} : {})}
    >
      <TextInput value={props.value} change={props.change} placeholder="/path/to/repository" mono />
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
  /** Send the last Start again, under the id it already has. */
  readonly retry: () => void;
  /** Show the run a Start created. */
  readonly openRun: (host: string, runId: string) => void;
}): JSX.Element {
  const {draft} = props;
  const host = props.hosts.find(candidate => candidate.key === props.chosen) ?? props.hosts[0];
  const set = (change: Partial<StartDraft>): void => props.setDraft({...draft, ...change});
  const choices = host === undefined ? NO_CATALOG : modelChoices(host.models, LEADS[draft.lead]);
  const model = startModel(draft.model, choices);
  const missing = host === undefined ? 'No host is connected.' : missingFrom(draft, choices);
  const verdict = repositoryVerdict(draft.repository, host?.repositoryChecked);
  const block = startBlock(props.progress?._tag === 'Sending', missing, verdict);
  const askAbout = (repository: string): void => {
    if (host !== undefined) props.checkRepository(host.key, repository);
  };
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
            }}
            mono
          />
        </Field>
      ) : null}
      <RepositoryField
        value={draft.repository}
        verdict={verdict}
        change={repository => {
          set({repository});
          askAbout(repository);
        }}
      />
      <Field label="Task" help="The task to optimize, as named in the repository.">
        <TextInput value={draft.task} change={task => set({task})} placeholder="task" mono />
      </Field>
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
      <Field label="Model">
        <Select
          value={model}
          options={modelOptions(choices)}
          change={chosen => set({model: chosen})}
          mono
        />
      </Field>
      <LimitFields draft={draft} set={set} />
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
            if (host !== undefined && block === null)
              props.start(host.key, startRunRequest(draft, choices));
          }}
          {...(block === null ? {} : {disabledReason: block.reason, quietReason: block.quiet})}
        />
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
