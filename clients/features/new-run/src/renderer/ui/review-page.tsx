/**
 * Step 2, Review and start: every other answer as a row, filled from the defaults (the last run on
 * the machine, the machine's default agent) and changed in place: what the run is and its goal,
 * where the agent works and results are measured, the agent, and the limits; beside them, what
 * must hold before the run can start, each with the fix it offers.
 */
import type {HarnessOffer, MeasureTarget} from '@vibesys/ipc';
import {
  Banner,
  Button,
  type CheckItem,
  CheckList,
  type CheckState,
  Field,
  Row,
  SegmentedControl,
  Select,
  Split,
  Stack,
  Text,
  type TextTone,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import {
  harnessLabel,
  measureOptions,
  type NewRunContext,
  type NewRunEvent,
  type NewRunSnapshot,
  nothingSetUp,
  roleHarnesses,
  roleModels,
} from '../../state/index.js';
import {IdeasField, TimeLimitField} from './budget-fields.js';
import type {CardProps} from './card-props.js';
import {backendLabel} from './cause-words.js';
import {GoalRow} from './goal-page.js';
import {MoreOptions} from './more-options.js';
import {problemWords} from './problem-words.js';
import {RoleAgents} from './role-agents.js';
import {pickedWords} from './steps.js';
import {
  backendOf,
  hardwareLine,
  type MachineOption,
  machineLabel,
  measureLabel,
  type ReadinessFix,
  readiness,
  readinessStatus,
  startBlocked,
} from './view.js';

type Dispatch = (event: NewRunEvent) => void;

/** What Review's fixes open outside New run. */
export interface ReviewLinks {
  /** Open Machines, where the machine's VibeSys checkout is chosen. */
  readonly chooseCheckout: () => void;
  /** Open Settings on the agent logins of `machine`. */
  readonly openLogins: (machine: string) => void;
  /** Open Settings on the clusters of `machine`, where a place to measure is set up. */
  readonly setUp: (machine: string) => void;
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
      return 'Open Agent logins';
    case 'recheck':
      return 'Check again';
  }
}

function fixPress(
  fix: ReadinessFix,
  dispatch: Dispatch,
  links: ReviewLinks,
  machine: string,
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
const TONES: Readonly<Record<CheckState, TextTone>> = {
  fail: 'err',
  warn: 'warn',
  pending: 'muted',
  ok: 'ok',
};

/** Why the last load, fix or start failed, in plain words, with a retry for a load or check. */
export function ProblemBanner(props: CardProps): JSX.Element | null {
  const {context, dispatch} = props;
  if (context.problem === null) return null;
  const words = problemWords(context.problem, machineLabel(context.form.machine));
  return (
    <Banner
      tone="err"
      title={words.title}
      body={words.body}
      {...(words.detail === null ? {} : {hint: words.detail})}
      {...(words.retry
        ? {actions: <Button text="Try again" press={() => dispatch({type: 'retry'})} />}
        : {})}
    />
  );
}

function Section(props: {readonly title: string; readonly children: ReactNode}): JSX.Element {
  return (
    <Stack gap="space-3">
      <Text variant="section">{props.title}</Text>
      {props.children}
    </Stack>
  );
}

function What(props: CardProps): JSX.Element {
  const {context, dispatch} = props;
  return (
    <Section title="What">
      <Field row label="Task">
        <Row justify="between" gap="space-3">
          <Text variant="code">{pickedWords(context) ?? 'Not chosen'}</Text>
          <Button
            variant="quiet"
            size="sm"
            text="Change"
            hint="Choose another task"
            press={() => dispatch({type: 'go', step: 'pick'})}
          />
        </Row>
      </Field>
      <Field row label="Goal">
        <GoalRow context={context} />
      </Field>
    </Section>
  );
}

/** A target's value in the select. */
function targetKey(target: MeasureTarget): string {
  return target.kind === 'slurm' ? `slurm:${target.cluster}` : target.kind;
}

function targetWords(target: MeasureTarget): string {
  return target.kind === 'same_machine' ? 'The same machine' : measureLabel(target);
}

function Where(
  props: CardProps & {readonly machines: readonly MachineOption[]; readonly links: ReviewLinks},
): JSX.Element {
  const {context, dispatch} = props;
  const {machine} = context.form;
  const name = machineLabel(machine);
  const facts = context.facts?.for === machine ? context.facts.value : null;
  const described = facts?.kind === 'offered' ? facts.value : null;
  const here = described === null ? name : `${name} · ${hardwareLine(described)}`;
  // A project of the user's is on the machine it was chosen on.
  const own = context.answers.from === 'project' && context.answers.project !== null;
  const machines = props.machines.some(option => option.value === machine)
    ? props.machines
    : [...props.machines, {value: machine, label: name}];
  const targets = measureOptions(context.compute);
  const backend = backendOf(context);
  return (
    <Section title="Where">
      <Field row label="Agent works on" {...(own ? {help: 'Where the project is'} : {})}>
        {own ? (
          <Text>{here}</Text>
        ) : (
          <Select
            value={machine}
            options={machines.map(option => ({
              value: option.value,
              label: option.value === machine ? here : option.label,
            }))}
            change={next => dispatch({type: 'machine', machine: next})}
          />
        )}
      </Field>
      <Field
        row
        label="Measured on"
        {...(backend === null ? {} : {help: `With ${backendLabel(backend)}, as the task says`})}
      >
        {targets.length < 2 ? (
          // Nothing else is set up: a select of one would offer no choice, so it says what there
          // is, and where more is set up.
          <Row justify="between" gap="space-3">
            <Text>{targetWords(context.form.measure)}</Text>
            {nothingSetUp(context.compute) ? (
              <Button
                variant="quiet"
                size="sm"
                text={`Set up a cluster on ${name}`}
                press={() => props.links.setUp(machine)}
              />
            ) : null}
          </Row>
        ) : (
          <Select
            value={targetKey(context.form.measure)}
            options={targets.map(target => ({
              value: targetKey(target),
              label: targetWords(target),
            }))}
            change={key => {
              const target = targets.find(each => targetKey(each) === key);
              if (target !== undefined) dispatch({type: 'measure', target});
            }}
          />
        )}
      </Field>
    </Section>
  );
}

/** The run's agent: its model, and the harness that runs it when more than one does. */
function AgentRows(
  props: CardProps & {readonly offers: readonly HarnessOffer[]},
): JSX.Element | null {
  const {context, dispatch, offers} = props;
  const {agent} = context.form;
  if (agent === null) return <Text tone="warn">No agents</Text>;
  const providers = [...new Set(offers.map(offer => offer.provider))];
  const models = roleModels(offers, providers, agent.provider);
  const harnesses = roleHarnesses(offers, providers, agent.model);
  return (
    <>
      <Field row label="Model">
        <Select
          value={JSON.stringify([agent.provider, agent.model])}
          options={models.map(model => ({
            value: model.key,
            label: model.label,
            group: model.group,
          }))}
          change={key => {
            const picked = models.find(model => model.key === key);
            if (picked === undefined) return;
            // The harness stays when it runs the model picked.
            const keep = roleHarnesses(offers, providers, picked.model).includes(agent.provider);
            dispatch({
              type: 'agent',
              provider: keep ? agent.provider : picked.provider,
              model: picked.model,
            });
          }}
        />
      </Field>
      <Field row label="Runs in">
        {harnesses.length > 1 ? (
          <SegmentedControl
            label="Runs in"
            size="sm"
            options={harnesses.map(provider => ({
              value: provider,
              label: harnessLabel(offers, provider),
            }))}
            value={agent.provider}
            change={provider => dispatch({type: 'agent', provider, model: agent.model})}
          />
        ) : (
          <Text>{harnessLabel(offers, agent.provider)}</Text>
        )}
      </Field>
      <RoleAgents {...props} offers={offers} agent={agent} />
    </>
  );
}

function Agent(props: CardProps): JSX.Element {
  const {context} = props;
  const {machine} = context.form;
  const offers = context.harnesses?.for === machine ? context.harnesses.list : null;
  return (
    <Section title="Agent">
      {offers === null ? (
        <Text tone="muted">Listing</Text>
      ) : (
        <AgentRows {...props} offers={offers} />
      )}
    </Section>
  );
}

function Limits(props: CardProps): JSX.Element {
  return (
    <Section title="Limits">
      <IdeasField {...props} />
      <TimeLimitField {...props} />
      <MoreOptions {...props} />
    </Section>
  );
}

/** What must hold before the run starts, worst first, each with the fix it offers. */
function BeforeYouStart(props: {
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
      <Text variant="section">Before you start</Text>
      <Text tone={TONES[status.state]} variant="body-strong">
        {status.text}
      </Text>
      <CheckList items={items} />
    </Stack>
  );
}

/** What Review's one-line entry in the step list says: what needs the user, else what it holds. */
export function reviewWords(snapshot: NewRunSnapshot): string {
  const failed = readiness(snapshot).filter(item => item.state === 'fail').length;
  if (failed === 0) return 'Machine, agent, limits';
  return failed === 1 ? '1 check needs you' : `${failed} checks need you`;
}

export function ReviewPage(
  props: {
    readonly snapshot: NewRunSnapshot;
    readonly dispatch: Dispatch;
    readonly machines: readonly MachineOption[];
  } & ReviewLinks,
): JSX.Element {
  const {snapshot, dispatch} = props;
  const context: NewRunContext = snapshot.context;
  const cardProps = {context, dispatch};
  const links: ReviewLinks = props;
  return (
    <Split
      main={
        <Stack gap="space-6">
          <What {...cardProps} />
          <Where {...cardProps} machines={props.machines} links={links} />
          <Agent {...cardProps} />
          <Limits {...cardProps} />
        </Stack>
      }
      aside={<BeforeYouStart snapshot={snapshot} dispatch={dispatch} links={links} />}
    />
  );
}

/**
 * Back, then why Start is not open yet beside Start itself, on one line: the reason never moves
 * the button. The button keeps the reason for assistive technology.
 */
export function ReviewNav(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {dispatch} = props;
  const blocked = startBlocked(props.snapshot);
  return (
    <Row justify="between" wrap>
      <Button text="Back" press={() => dispatch({type: 'back'})} />
      <Row gap="space-3">
        {blocked === undefined ? null : <Text tone="muted">{blocked}</Text>}
        <Button
          variant="primary"
          text="Start run"
          press={() => dispatch({type: 'start'})}
          {...(blocked === undefined ? {} : {disabledReason: blocked, quietReason: true})}
        />
      </Row>
    </Row>
  );
}
