/**
 * The Review page: every answer with a way to change it, the checks of the run on its machine
 * (with the fixes they offer), the options most runs leave alone, and Start.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  Banner,
  Button,
  type CheckItem,
  CheckList,
  type CheckState,
  Row,
  Stack,
  Text,
  type TextTone,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  chosenEntry,
  exampleLabel,
  hardwareLabel,
  type NewRunContext,
  type NewRunEvent,
  type NewRunSnapshot,
  roleLabel,
  type StepId,
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
    case 'recheck':
      return 'Check again';
  }
}

function fixPress(
  fix: ReadinessFix,
  dispatch: Dispatch,
  chooseCheckout: () => void,
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
      return chooseCheckout;
    case 'recheck':
      return () => dispatch({type: 'refresh'});
  }
}

const RANK: Readonly<Record<CheckState, number>> = {fail: 0, warn: 1, pending: 2, ok: 3};
const TONES: Readonly<Record<CheckState, TextTone>> = {
  fail: 'err',
  warn: 'warn',
  pending: 'muted',
  ok: 'ok',
};

interface Answer {
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
      label: 'Task',
      value:
        entry === null
          ? (form.task ?? 'Not chosen')
          : `${exampleLabel(entry.example)} · ${entry.task}`,
      step: taskStep(context),
    },
    ...(target === null || target.vendor === 'cpu'
      ? []
      : [{label: 'Tuned for', value: hardwareLabel(target), step: 'hardware' as const}]),
    {label: 'Agent works on', value: machineLabel(form.machine), step: 'where'},
    {
      label: 'Measured on',
      value: `${measureLabel(form.measure)}${backend === null ? '' : `, ${backendLabel(backend)}`}`,
      step: 'where',
    },
    {label: 'Agent', value: agentWords(context) ?? 'Not chosen', step: 'agent'},
    ...roleAgentsChanged(context).map(({role, agent}) => ({
      label: `${roleLabel(role)} agent`,
      value: pickWords(context, agent) ?? `${agent.model} · ${agent.provider}`,
      step: 'agent' as const,
    })),
    {
      label: 'Ideas to try',
      value: rounds === null ? 'As the task is configured' : String(rounds),
      step: 'agent',
    },
  ];
}

function Summary(props: {
  readonly context: NewRunContext;
  readonly dispatch: Dispatch;
}): JSX.Element {
  return (
    <Stack gap="space-3">
      {answers(props.context).map(answer => (
        <Row key={answer.label} justify="between" wrap>
          <Stack gap="space-1">
            <Text variant="small" tone="muted">
              {answer.label}
            </Text>
            <Text variant="body-strong">{answer.value}</Text>
          </Stack>
          <Button
            variant="quiet"
            size="sm"
            text="Change"
            hint={`Change ${answer.label.toLowerCase()}: ${STEP_TITLES[answer.step]}`}
            press={() => props.dispatch({type: 'go', step: answer.step})}
          />
        </Row>
      ))}
    </Stack>
  );
}

/** Why the last load, fix or start failed, in plain words, with a retry for a load or check. */
export function ProblemBanner(props: {
  readonly context: NewRunContext;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {context, dispatch} = props;
  if (context.problem === null) return null;
  const words = problemWords(context.problem, machineLabel(context.form.machine));
  return (
    <Banner
      tone="failed"
      title={words.title}
      body={words.body}
      {...(words.detail === null ? {} : {hint: words.detail})}
      {...(words.retry
        ? {actions: <Button text="Try again" press={() => dispatch({type: 'retry'})} />}
        : {})}
    />
  );
}

/** The checks of the run on its machine, worst first, each with the fix it offers. */
function Checks(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly chooseCheckout: () => void;
}): JSX.Element {
  const {snapshot, dispatch, chooseCheckout} = props;
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
              press: fixPress(fix, dispatch, chooseCheckout, snapshot.context.form.machine),
            },
          }),
    }));
  const status = readinessStatus(checks);
  return (
    <Stack gap="space-3">
      <Text variant="section">Ready to start</Text>
      <Text tone={TONES[status.state]} variant="body-strong">
        {status.text}
      </Text>
      <CheckList items={items} />
    </Stack>
  );
}

export function ReviewPage(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly chooseCheckout: () => void;
}): JSX.Element {
  const {snapshot, dispatch} = props;
  const {context} = snapshot;
  const cardProps = {context, dispatch};
  return (
    <Stack gap="space-5">
      <Summary {...cardProps} />
      <Checks {...props} />
      <MoreOptions {...cardProps} />
    </Stack>
  );
}

/** Start, and why it is not open yet. */
export function StartButton(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const blocked = startBlocked(props.snapshot);
  return (
    <Button
      variant="primary"
      text="Start run"
      press={() => props.dispatch({type: 'start'})}
      {...(blocked === undefined ? {} : {disabledReason: blocked, quietReason: true})}
    />
  );
}
