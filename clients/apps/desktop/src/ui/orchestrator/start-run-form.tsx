import {Button, Field, Row, Select, Stack, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CommandProgress} from '../../state/index.js';
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
} from './start-draft.js';
import {harnessWord} from './words.js';

/** A host the run can start on: its key, and what it is called on screen. */
export interface StartHost {
  readonly key: string;
  readonly label: string;
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
  /** Send the last Start again, under the id it already has. */
  readonly retry: () => void;
  /** Show the run a Start created. */
  readonly openRun: (host: string, runId: string) => void;
}): JSX.Element {
  const {draft} = props;
  const host = props.hosts.find(candidate => candidate.key === props.chosen) ?? props.hosts[0];
  const set = (change: Partial<StartDraft>): void => props.setDraft({...draft, ...change});
  const missing = host === undefined ? 'No host is connected.' : missingFrom(draft);
  const sending = props.progress?._tag === 'Sending';
  const reason = sending ? 'Sending.' : missing;
  return (
    <Stack gap="space-3">
      {props.hosts.length > 1 ? (
        <Field label="Host">
          <Select
            value={host?.key ?? ''}
            options={props.hosts.map(option => ({value: option.key, label: option.label}))}
            change={props.choose}
            mono
          />
        </Field>
      ) : null}
      <Field label="Repository" help="The path of the repository on the host.">
        <TextInput
          value={draft.repository}
          change={repository => set({repository})}
          placeholder="/path/to/repository"
          mono
        />
      </Field>
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
          change={(choice: LeadChoice) => set({lead: choice})}
        />
      </Field>
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
            if (host !== undefined && missing === null)
              props.start(host.key, startRunRequest(draft));
          }}
          {...(reason === null ? {} : {disabledReason: reason, quietReason: sending})}
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
