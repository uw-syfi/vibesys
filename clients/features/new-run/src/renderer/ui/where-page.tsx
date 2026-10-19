/**
 * The Where it runs page: the machine the agent works on, and where results are measured: that
 * machine, the Slurm clusters and Modal set up on it (from its Settings data), with a way to Settings
 * to set one up when it has none. Whether the machine has the hardware the task targets is said here
 * and again, with the other checks, on Review.
 */

import type {MeasureTarget} from '@vibesys/ipc';
import {
  Banner,
  Button,
  ChoiceTiles,
  Collapsible,
  KeyValue,
  Row,
  Stack,
  Text,
  type TileOption,
} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  chosenEntry,
  fitOf,
  measureOptions,
  missingFeatures,
  nothingSetUp,
  targetFacts,
} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {
  entriesIn,
  hardwareLine,
  type MachineOption,
  machineFacts,
  machineLabel,
  measureLabel,
  NEEDS_UPDATE,
} from './view.js';

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

/** What is wrong with the chosen machine's VibeSys: no checkout, or too old for this app. */
function MachineNotices(props: CardProps & {readonly chooseCheckout: () => void}): JSX.Element {
  const {context} = props;
  const {machine} = context.form;
  const name = machineLabel(machine);
  const facts = context.facts?.for === machine ? context.facts.value : null;
  const noCheckout = facts?.kind === 'offered' && facts.value.checkout === null;
  return (
    <>
      {missingFeatures(context.helper, machine).length > 0 ? (
        <Banner tone="warn" title={`VibeSys on ${name}`} body={NEEDS_UPDATE} />
      ) : null}
      {noCheckout ? (
        <Banner
          tone="warn"
          title={`No VibeSys checkout on ${name}`}
          actions={<Button text="Choose it in Machines" press={props.chooseCheckout} />}
        />
      ) : null}
    </>
  );
}

/** A target's value in the tiles. */
function targetKey(target: MeasureTarget): string {
  return target.kind === 'slurm' ? `slurm:${target.cluster}` : target.kind;
}

export function WherePage(
  props: CardProps & {
    readonly machines: readonly MachineOption[];
    readonly chooseCheckout: () => void;
    /** Open Settings on the machine's clusters. */
    readonly setUp: () => void;
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
      ? {note: described === null ? 'Checking' : hardwareLine(described)}
      : {}),
    ...(ownProject && option.value !== machine ? {unavailable: `Project on ${name}`} : {}),
  }));
  const targets = measureOptions(context.compute);
  const where: TileOption<string>[] = targets.map(target => ({
    value: targetKey(target),
    label: measureLabel(target),
    ...(target.kind === 'same_machine' && described !== null
      ? {meta: hardwareLine(described)}
      : {}),
  }));
  const needs = chosenEntry(context.answers, entriesIn(context))?.details?.hardware ?? null;
  const fit = needs === null ? null : fitOf(needs, described, name, context.form.measure);
  return (
    <Stack gap="space-5">
      <Stack gap="space-3">
        <Text variant="section">Agent works on</Text>
        <ChoiceTiles
          label="Agent works on"
          layout="cards"
          options={machines}
          value={machine}
          change={next => dispatch({type: 'machine', machine: next})}
        />
        <MachineNotices {...props} />
      </Stack>
      <Stack gap="space-3">
        <Text variant="section">Measured on</Text>
        <ChoiceTiles
          label="Measured on"
          layout="cards"
          options={where}
          value={targetKey(context.form.measure)}
          change={key => {
            const target = targets.find(each => targetKey(each) === key);
            if (target !== undefined) dispatch({type: 'measure', target});
          }}
        />
        <KeyValue items={targetFacts(context.compute, context.form.measure)} />
        {nothingSetUp(context.compute) ? (
          <Row>
            <Button text={`Set up on ${name}`} press={props.setUp} />
          </Row>
        ) : null}
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
