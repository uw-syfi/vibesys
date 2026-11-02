/**
 * The Review page: every answer with a way to change it, the checks of the run on its machine
 * (with the fixes they offer), the options most runs leave alone, and Start.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  Button,
  type CheckItem,
  CheckList,
  type CheckState,
  Chip,
  LabeledRow,
  Row,
  Split,
  Stack,
  type StatusTone,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  chosenEntry,
  exampleLabel,
  hardwareLabel,
  type NewRunContext,
  type NewRunEvent,
  type NewRunSnapshot,
  OPEN_LOGINS_WORDS,
  roleLabel,
  type StepId,
  startUnconfirmed,
  stepIds,
} from '../../state/index.js';
import {backendLabel} from './cause-words.js';
import {MoreOptions} from './more-options.js';
import {problemWords} from './problem-words.js';
import {agentWords, pickWords, STEP_TITLES} from './steps.js';
import {
  backendOf,
  entriesIn,
  machineLabel,
  measureLabel,
  type ReadinessFix,
  readiness,
  readinessStatus,
  roleAgentsChanged,
  startBlocked,
} from './view.js';

type Dispatch = (event: NewRunEvent) => void;

/** The ways out of Review into other screens. */
export interface ReviewLinks {
  /** Open Machines, where the machine's VibeSys checkout is chosen. */
  readonly chooseCheckout: () => void;
  /** Open Settings on `machine`'s Agent logins. */
  readonly openLogins: (machine: HostKey) => void;
}

function fixText(fix: ReadinessFix): string {
  switch (fix.kind) {
    case 'copy_out':
      return 'Copy to its own repository';
    case 'create_from_example':
      return 'Create the project';
    case 'use_created':
      return 'Use it';
    case 'rename_destination':
      return 'Choose another name';
    case 'choose_checkout':
      return 'Choose it in Machines';
    case 'open_logins':
      return OPEN_LOGINS_WORDS;
    case 'recheck':
      return 'Check again';
  }
}

function fixPress(
  fix: ReadinessFix,
  dispatch: Dispatch,
  links: ReviewLinks,
  machine: HostKey,
): () => void {
  switch (fix.kind) {
    case 'copy_out':
    case 'create_from_example':
      return () => dispatch({type: 'fix', fix});
    case 'use_created':
      return () =>
        dispatch({
          type: 'use_created',
          example: fix.example,
          destination: fix.destination,
          path: fix.path,
        });
    case 'rename_destination':
      // The destination field is in More options.
      return () => dispatch({type: 'more_options', machine, open: true});
    case 'choose_checkout':
      return links.chooseCheckout;
    case 'open_logins':
      return () => links.openLogins(machine);
    case 'recheck':
      return () => dispatch({type: 'refresh'});
  }
}

const RANK: Readonly<Record<CheckState, number>> = {fail: 0, warn: 1, pending: 2, ok: 3};
/** The tone the one-line state of the checks is drawn in: a check being made is in progress. */
const TONES: Readonly<Record<CheckState, StatusTone>> = {
  fail: 'err',
  warn: 'warn',
  pending: 'live',
  ok: 'ok',
};

type Section = 'What' | 'Where' | 'Agent' | 'Limits';

interface Answer {
  readonly section: Section;
  readonly label: string;
  readonly value: string;
  readonly step: StepId;
}

/** The step that completed the task: the last question before the goal. */
function taskStep(context: NewRunContext): StepId {
  const ids = stepIds(context.answers, entriesIn(context));
  return ids[ids.indexOf('goal') - 1] ?? 'start';
}

/** What the run is, answer by answer, each with the step that changes it. */
function answers(context: NewRunContext): Answer[] {
  const {form} = context;
  const entry = chosenEntry(context.answers, entriesIn(context));
  const target = entry?.details?.hardware ?? null;
  const backend = backendOf(context);
  const rounds = form.budget.rounds;
  return [
    {
      section: 'What',
      label: 'Task',
      value:
        entry === null
          ? (form.task ?? 'Not chosen')
          : `${exampleLabel(entry.example)} · ${entry.task}`,
      step: taskStep(context),
    },
    ...(target === null || target.vendor === 'cpu'
      ? []
      : [
          {
            section: 'What' as const,
            label: 'Tuned for',
            value: hardwareLabel(target),
            step: 'hardware' as const,
          },
        ]),
    {section: 'Where', label: 'Agent works on', value: machineLabel(form.machine), step: 'where'},
    {
      section: 'Where',
      label: 'Measured on',
      value: `${measureLabel(form.measure)}${backend === null ? '' : `, ${backendLabel(backend)}`}`,
      step: 'where',
    },
    {section: 'Agent', label: 'Model', value: agentWords(context) ?? 'Not chosen', step: 'agent'},
    ...roleAgentsChanged(context).map(({role, agent}) => ({
      section: 'Agent' as const,
      label: `${roleLabel(role)} agent`,
      value: pickWords(context, agent) ?? `${agent.model} · ${agent.provider}`,
      step: 'agent' as const,
    })),
    {
      section: 'Limits',
      label: 'Ideas to try',
      value: rounds === null ? 'As the task is configured' : String(rounds),
      step: 'agent',
    },
  ];
}

const SECTIONS: readonly Section[] = ['What', 'Where', 'Agent', 'Limits'];

/** The answers under What, Where, Agent and Limits, each row with the step that changes it. */
function Summary(props: {
  readonly context: NewRunContext;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const all = answers(props.context);
  return (
    <Stack gap="space-5">
      {SECTIONS.map(section => (
        <Stack key={section} gap="space-3">
          <Text variant="section">{section}</Text>
          {all
            .filter(answer => answer.section === section)
            .map(answer => (
              <LabeledRow key={answer.label} label={answer.label}>
                <Row justify="between" gap="space-3">
                  <Text variant="body-strong">{answer.value}</Text>
                  <Button
                    variant="quiet"
                    size="sm"
                    text="Change"
                    hint={`Change ${answer.label.toLowerCase()}: ${STEP_TITLES[answer.step]}`}
                    press={() => props.dispatch({type: 'go', step: answer.step})}
                  />
                </Row>
              </LabeledRow>
            ))}
        </Stack>
      ))}
    </Stack>
  );
}

/**
 * Why the last load, fix or start failed, in a few words, the account in the chip's description.
 * A load or check has a retry. A start that may have happened says so on the page and sends the
 * person to their runs, which is also what opens Start again.
 */
export function ProblemStatus(props: {
  readonly context: NewRunContext;
  readonly dispatch: Dispatch;
  /** Open the Runs screen, where a run that did start is listed. */
  readonly showRuns: () => void;
}): JSX.Element | null {
  const {context, dispatch} = props;
  if (context.problem === null) return null;
  const words = problemWords(context.problem, machineLabel(context.form.machine));
  const account = [words.title, words.body, words.detail].filter(each => each !== null).join('\n');
  return (
    <Row gap="space-3" align="center" wrap>
      <Chip label={words.label} tone={words.look ? 'warn' : 'err'} dot hint={account} />
      {words.retry ? <Button text="Try again" press={() => dispatch({type: 'retry'})} /> : null}
      {words.look ? (
        <Button
          text="Open Runs"
          press={() => {
            dispatch({type: 'looked'});
            props.showRuns();
          }}
        />
      ) : null}
    </Row>
  );
}

/** The checks of the run on its machine, worst first, each with the fix it offers. */
function Checks(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly links: ReviewLinks;
}): JSX.Element {
  const {snapshot, dispatch, links} = props;
  const checks = readiness(snapshot);
  const items: CheckItem[] = [...checks]
    .sort((left, right) => RANK[left.state] - RANK[right.state])
    .map(({fix, ...item}) => ({
      ...item,
      ...(fix === undefined
        ? {}
        : {
            fix: {
              text: fixText(fix),
              press: fixPress(fix, dispatch, links, snapshot.context.form.machine),
            },
          }),
    }));
  const status = readinessStatus(checks);
  return (
    <Stack gap="space-3">
      <Row gap="space-3" align="center" wrap>
        <Text variant="section">Before you start</Text>
        <Chip label={status.text} tone={TONES[status.state]} dot />
      </Row>
      <CheckList items={items} />
    </Stack>
  );
}

export function ReviewPage(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly links: ReviewLinks;
}): JSX.Element {
  const {snapshot, dispatch} = props;
  const {context} = snapshot;
  const cardProps = {context, dispatch};
  return (
    <Split
      main={
        <Stack gap="space-5">
          <Summary {...cardProps} />
          <MoreOptions {...cardProps} />
        </Stack>
      }
      aside={<Checks {...props} />}
    />
  );
}

/** Start, and why it is not open yet. */
export function StartButton(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const blocked = startBlocked(props.snapshot);
  // After a start that may have happened the reason is drawn beside Start, not only described.
  const quiet = !startUnconfirmed(props.snapshot.context.problem);
  return (
    <Button
      variant="primary"
      text="Start run"
      press={() => props.dispatch({type: 'start'})}
      {...(blocked === undefined ? {} : {disabledReason: blocked, quietReason: quiet})}
    />
  );
}
