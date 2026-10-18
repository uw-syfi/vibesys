/**
 * The Where it runs page: the machine the agent works on, and where results are measured. Only
 * measuring on the same machine exists today; clusters and clouds are shown as what they will be,
 * with the plain reason they cannot be chosen yet. Whether the machine has the hardware the task
 * targets is said here and again, with the other checks, on Review.
 */
import {
  Banner,
  Button,
  ChoiceTiles,
  Collapsible,
  KeyValue,
  Stack,
  Text,
  type TileOption,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {chosenEntry, fitOf} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {entriesIn, hardwareLine, type MachineOption, machineFacts, machineLabel} from './view.js';

type Where = 'same' | 'slurm' | 'modal' | 'skypilot';

const SETTINGS_SOON = 'Setting this up comes with each machine’s Settings.';

function MachineDetails(props: CardProps): JSX.Element | null {
  const {context} = props;
  const [open, setOpen] = useState(false);
  const facts = context.facts?.for === context.form.machine ? context.facts.value : null;
  if (facts === null || facts.kind !== 'offered') return null;
  return (
    <Collapsible
      label="Machine details"
      open={open}
      toggle={setOpen}
      {...(open ? {} : {summary: hardwareLine(facts.value)})}
    >
      <KeyValue items={machineFacts(facts.value)} />
    </Collapsible>
  );
}

export function WherePage(
  props: CardProps & {
    readonly machines: readonly MachineOption[];
    readonly chooseCheckout: () => void;
  },
): JSX.Element {
  const {context, dispatch} = props;
  const {machine} = context.form;
  const name = machineLabel(machine);
  const facts = context.facts?.for === machine ? context.facts.value : null;
  const described = facts?.kind === 'offered' ? facts.value : null;
  const ownProject = context.answers.from === 'project' && context.answers.project !== null;
  const machines: TileOption<string>[] = (
    props.machines.some(option => option.value === machine)
      ? props.machines
      : [...props.machines, {value: machine, label: name}]
  ).map(option => ({
    value: option.value,
    label: option.label,
    ...(option.value === machine
      ? {note: described === null ? 'Asking what it offers.' : hardwareLine(described)}
      : {}),
    ...(ownProject && option.value !== machine ? {unavailable: `Your project is on ${name}.`} : {}),
  }));
  const where: TileOption<Where>[] = [
    {
      value: 'same',
      label: 'Same machine',
      note: `${name}, where the agent works.`,
      ...(described === null ? {} : {meta: hardwareLine(described)}),
    },
    {value: 'slurm', label: 'Slurm cluster', note: 'Batch jobs.', unavailable: SETTINGS_SOON},
    {value: 'modal', label: 'Modal', note: 'Cloud GPUs.', unavailable: SETTINGS_SOON},
    {
      value: 'skypilot',
      label: 'SkyPilot',
      note: 'Any cloud or cluster profile.',
      unavailable: 'Preview. Not available yet.',
    },
  ];
  const needs = chosenEntry(context.answers, entriesIn(context))?.details?.hardware ?? null;
  const fit = needs === null ? null : fitOf(needs, described, name);
  const noCheckout = described !== null && described.checkout === null;
  return (
    <Stack gap="space-5">
      <Stack gap="space-3">
        <Text variant="section">Agent works on</Text>
        <Text tone="muted">The agent edits and builds the code on this machine.</Text>
        <ChoiceTiles
          label="Agent works on"
          layout="cards"
          options={machines}
          value={machine}
          change={next => dispatch({type: 'machine', machine: next})}
        />
        {noCheckout ? (
          <Banner
            tone="warn"
            title={`No VibeSys checkout found on ${name}.`}
            actions={<Button text="Choose it in Machines" press={props.chooseCheckout} />}
          />
        ) : null}
      </Stack>
      <Stack gap="space-3">
        <Text variant="section">Measured on</Text>
        <Text tone="muted">Where each candidate is measured.</Text>
        <ChoiceTiles
          label="Measured on"
          layout="cards"
          options={where}
          value="same"
          change={() => undefined}
        />
      </Stack>
      {fit === null || fit.kind === 'unknown' ? null : (
        <Banner
          tone={fit.kind === 'fits' ? 'ok' : 'warn'}
          title={fit.kind === 'fits' ? 'The hardware fits.' : 'The hardware may not fit.'}
          body={fit.words}
        />
      )}
      <MachineDetails {...props} key={machine} />
    </Stack>
  );
}
