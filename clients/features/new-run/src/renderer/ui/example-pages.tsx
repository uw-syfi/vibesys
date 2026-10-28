/**
 * The examples of the pick page: one row per example task, filtered by domain and a search, with
 * what each is, what it needs and whether the machine the form is on has it. A row picked is the
 * task the run is of. When the machine cannot list its examples the pane says so and offers the
 * other machines.
 */
import {
  Button,
  ChoiceTiles,
  Row,
  SegmentedControl,
  Stack,
  Table,
  type TableColumn,
  Text,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CatalogueEntry,
  type Domain,
  domainFilters,
  exampleLabel,
  examplesStatus,
  fitOf,
  fitWords,
  needsWords,
  shownEntries,
  whatItIs,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {entriesIn, type MachineOption, machineLabel} from './view.js';

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
function ExamplesFailed(
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

/** A row's key: the example and its task. */
function entryKey(entry: {readonly example: string; readonly task: string}): string {
  return `${entry.example}\n${entry.task}`;
}

/** The example's name, and below it its task unless it has only `default`: two short lines. */
function ExampleCell(entry: CatalogueEntry): JSX.Element {
  return (
    <Stack gap="space-0">
      <span>{exampleLabel(entry.example)}</span>
      {entry.task === 'default' ? null : (
        <Text variant="code" tone="muted">
          {entry.task}
        </Text>
      )}
    </Stack>
  );
}

function columns(props: CardProps): TableColumn<CatalogueEntry>[] {
  const {context} = props;
  const {machine, measure} = context.form;
  const name = machineLabel(machine);
  const facts = context.facts?.for === machine ? context.facts.value : null;
  const described = facts?.kind === 'offered' ? facts.value : null;
  return [
    {key: 'example', header: 'Example', lead: true, mono: true, cell: ExampleCell},
    {key: 'what', header: 'What it is', cell: whatItIs},
    // In a narrow table the hardware gives way first: a served model's name already says it.
    {key: 'needs', header: 'Needs', nowrap: true, wide: true, cell: needsWords},
    {
      key: 'fit',
      header: `On ${name}`,
      wide: true,
      nowrap: true,
      cell: entry => {
        const needs = entry.details?.hardware ?? null;
        const fit = fitWords(
          fitOf(needs, described, name, measure),
          name,
          described?.hardware ?? null,
        );
        if (fit === null) return null;
        return (
          <Text tone={fit.fits ? 'ok' : 'muted'}>{fit.fits ? `✓ ${fit.text}` : fit.text}</Text>
        );
      },
    },
  ];
}

/** The examples of the machine as a table, with its domain filter; a row picked is the run's task. */
export function ExamplesPane(
  props: CardProps & {
    readonly machines: readonly MachineOption[];
    readonly domain: Domain | null;
    readonly changeDomain: (domain: Domain | null) => void;
    readonly search: string;
  },
): JSX.Element {
  const {context, dispatch} = props;
  const status = examplesStatus(context.examples, context.form.machine);
  if (status === 'failed') return <ExamplesFailed {...props} />;
  if (status !== 'listed') {
    return <Text tone="muted">{status === 'listing' ? 'Listing' : 'No examples'}</Text>;
  }
  const entries = entriesIn(context);
  const filters = domainFilters(entries);
  const shown = shownEntries(entries, props.domain, props.search);
  const picked = context.answers.from === 'example' ? context.answers.entry : null;
  return (
    <Stack gap="space-3">
      {filters.length > 2 ? (
        <SegmentedControl
          label="Domain"
          size="sm"
          options={filters.map(filter => ({
            value: filter.domain ?? 'all',
            label: `${filter.label} ${filter.count}`,
          }))}
          value={props.domain ?? 'all'}
          change={value =>
            props.changeDomain(filters.find(filter => filter.domain === value)?.domain ?? null)
          }
        />
      ) : null}
      {shown.length === 0 ? (
        <Text tone="muted">No example matches</Text>
      ) : (
        <Table
          label="Examples"
          columns={columns(props)}
          rows={shown}
          rowKey={entryKey}
          {...(picked === null ? {} : {selected: entryKey(picked)})}
          select={key => {
            const entry = shown.find(each => entryKey(each) === key);
            if (entry !== undefined) {
              dispatch({
                type: 'answer',
                change: {kind: 'entry', example: entry.example, task: entry.task},
              });
            }
          }}
        />
      )}
    </Stack>
  );
}
