/**
 * The pages that ask what to optimize: start from an example or the user's own project, then, for
 * an example, its domain and the questions that domain asks (a model and its hardware; a data
 * structure and its workload; an example) and, when several remain, which task.
 */
import {
  BrandMark,
  type BrandMarkName,
  Button,
  ChoiceTiles,
  Row,
  Stack,
  Text,
  type TileOption,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type AnswerChange,
  allExamples,
  DOMAIN_WORDS,
  domainOptions,
  entriesFor,
  exampleLabel,
  examplesOf,
  examplesStatus,
  hardwareGroups,
  hasDetails,
  MAKER_NAMES,
  modelGroups,
  openEntries,
  tasksOfExample,
  VENDOR_NAMES,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {backendLabel} from './cause-words.js';
import {entriesIn, type MachineOption, modelMakerMark, recentChoices, vendorMark} from './view.js';

function answer(props: CardProps, change: AnswerChange): void {
  props.dispatch({type: 'answer', change});
}

/** Said in place of options while the machine's examples are not known yet, or when it has none. */
function Listing(props: CardProps): JSX.Element {
  const {context} = props;
  const status = examplesStatus(context.examples, context.form.machine);
  return <Text tone="muted">{status === 'listing' ? 'Listing' : 'No examples'}</Text>;
}

/** The machines to list examples from: choosing one lists its examples (and tries a failed one again). */
function MachineChoice(
  props: CardProps & {readonly machines: readonly MachineOption[]},
): JSX.Element {
  const {machine} = props.context.form;
  return (
    <Stack gap="space-3">
      <Text variant="section">Machine</Text>
      <ChoiceTiles
        label="Machine"
        layout="cards"
        options={props.machines.map(option => ({value: option.value, label: option.label}))}
        value={machine}
        change={next => props.dispatch({type: 'machine', machine: next})}
      />
    </Stack>
  );
}

/**
 * Said when the machine's examples could not be listed: the status, a way to ask again, and the
 * other machines, so a broken one is never the end of the form.
 */
export function ExamplesFailed(
  props: CardProps & {readonly machines: readonly MachineOption[]},
): JSX.Element {
  const {context, dispatch} = props;
  const {machine} = context.form;
  return (
    <Stack gap="space-5">
      <Row gap="space-3">
        <Text tone="err">Failed</Text>
        <Button text="Try again" press={() => dispatch({type: 'machine', machine})} />
      </Row>
      <MachineChoice {...props} />
    </Stack>
  );
}

/** Whether the machine's examples are not listed yet, or it has none. */
function waitingForExamples(props: CardProps): boolean {
  return entriesIn(props.context).length === 0;
}

/** A group's name, with its official mark when `simple-icons` has one (else the name alone). */
function GroupLabel(props: {
  readonly text: string;
  readonly mark: BrandMarkName | null;
}): JSX.Element {
  return (
    <Row gap="space-2">
      {props.mark === null ? null : <BrandMark mark={props.mark} />}
      <Text variant="section">{props.text}</Text>
    </Row>
  );
}

/** The first question; when the machine's examples failed it also offers the other machines. */
export function StartPage(
  props: CardProps & {readonly machines: readonly MachineOption[]},
): JSX.Element {
  const {context} = props;
  const entries = entriesIn(context);
  const status = examplesStatus(context.examples, context.form.machine);
  const examples = allExamples(entries).length;
  const recents = recentChoices(context.recent).length;
  const options: TileOption<'example' | 'project'>[] = [
    {
      value: 'example',
      label: 'An example',
      ...(status === 'failed'
        ? {unavailable: 'Failed'}
        : status === 'none'
          ? {unavailable: 'No examples'}
          : {meta: examples === 0 ? 'Listing' : `${examples} examples`}),
    },
    {
      value: 'project',
      label: 'Your project',
      meta: `${recents} recent`,
    },
  ];
  return (
    <Stack gap="space-5">
      <ChoiceTiles
        label="Start from"
        layout="cards"
        options={options}
        value={context.answers.from}
        change={from => answer(props, {kind: 'from', from})}
      />
      {status === 'failed' ? <MachineChoice {...props} /> : null}
    </Stack>
  );
}

export function DomainPage(props: CardProps): JSX.Element {
  if (waitingForExamples(props)) return <Listing {...props} />;
  const options = domainOptions(entriesIn(props.context)).map(({domain, count}) => ({
    value: domain,
    label: DOMAIN_WORDS[domain].name,
    meta: `${count} ${count === 1 ? 'example' : 'examples'}`,
  }));
  return (
    <ChoiceTiles
      label="Domain"
      layout="cards"
      options={options}
      value={props.context.answers.domain}
      change={domain => answer(props, {kind: 'domain', domain})}
    />
  );
}

export function ModelPage(props: CardProps): JSX.Element {
  const groups = modelGroups(entriesIn(props.context));
  if (groups.length === 0) return <Listing {...props} />;
  return (
    <Stack gap="space-5">
      {groups.map(group => (
        <Stack key={group.maker} gap="space-3">
          <GroupLabel text={MAKER_NAMES[group.maker]} mark={modelMakerMark(group.maker)} />
          <ChoiceTiles
            label={MAKER_NAMES[group.maker]}
            layout="cards"
            options={group.models.map(model => ({value: model.name, label: model.name}))}
            value={
              group.models.some(m => m.name === props.context.answers.model)
                ? props.context.answers.model
                : null
            }
            change={model => answer(props, {kind: 'model', model})}
          />
        </Stack>
      ))}
    </Stack>
  );
}

export function HardwarePage(props: CardProps): JSX.Element {
  const {answers} = props.context;
  const entries = entriesIn(props.context);
  const model = answers.model ?? '';
  return (
    <Stack gap="space-5">
      {hardwareGroups(entries, model).map(group => (
        <Stack key={group.vendor} gap="space-3">
          <GroupLabel text={VENDOR_NAMES[group.vendor]} mark={vendorMark(group.vendor)} />
          <ChoiceTiles
            label={VENDOR_NAMES[group.vendor]}
            layout="cards"
            options={group.options.map(option => {
              const example = entriesFor(entries, model, option.key)[0]?.example;
              return {
                value: option.key,
                label: option.label,
                ...(option.backend === null ? {} : {note: backendLabel(option.backend)}),
                ...(example === undefined ? {} : {meta: exampleLabel(example)}),
              };
            })}
            value={group.options.some(o => o.key === answers.hardware) ? answers.hardware : null}
            change={hardware => answer(props, {kind: 'hardware', hardware})}
          />
        </Stack>
      ))}
    </Stack>
  );
}

export function StructurePage(props: CardProps): JSX.Element {
  const entries = entriesIn(props.context);
  return (
    <ChoiceTiles
      label="Structure"
      layout="cards"
      options={examplesOf(entries, 'data-structures').map(example => {
        const count = tasksOfExample(entries, example).length;
        return {
          value: example,
          label: exampleLabel(example),
          mono: true,
          meta: `${count} ${count === 1 ? 'workload' : 'workloads'}`,
        };
      })}
      value={props.context.answers.structure}
      change={example => answer(props, {kind: 'structure', example})}
    />
  );
}

export function WorkloadPage(props: CardProps): JSX.Element {
  const {answers} = props.context;
  const entries = tasksOfExample(entriesIn(props.context), answers.structure ?? '');
  return (
    <ChoiceTiles
      label="Workload"
      layout="cards"
      options={entries.map(entry => ({
        value: entry.task,
        label: entry.details?.workload?.name ?? entry.task,
        meta: entry.task,
      }))}
      value={answers.workload}
      change={task => answer(props, {kind: 'workload', task})}
    />
  );
}

export function ExamplePage(props: CardProps): JSX.Element {
  const {answers} = props.context;
  const entries = entriesIn(props.context);
  if (waitingForExamples(props)) return <Listing {...props} />;
  const names = hasDetails(entries)
    ? examplesOf(entries, answers.domain ?? 'databases')
    : allExamples(entries);
  return (
    <ChoiceTiles
      label="Example"
      layout="cards"
      options={names.map(example => {
        const tasks = tasksOfExample(entries, example);
        return {
          value: example,
          label: exampleLabel(example),
          mono: true,
          ...(tasks[0] === undefined ? {} : {note: tasks[0].category}),
          meta: `${tasks.length} ${tasks.length === 1 ? 'task' : 'tasks'}`,
        };
      })}
      value={answers.example}
      change={example => answer(props, {kind: 'example', example})}
    />
  );
}

export function VariantPage(props: CardProps): JSX.Element {
  const {answers} = props.context;
  return (
    <ChoiceTiles
      label="Task"
      layout="cards"
      options={openEntries(answers, entriesIn(props.context)).map(entry => ({
        value: entry.task,
        label: entry.task,
        mono: true,
      }))}
      value={answers.variant}
      change={task => answer(props, {kind: 'variant', task})}
    />
  );
}
