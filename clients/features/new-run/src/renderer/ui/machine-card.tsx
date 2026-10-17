/** The Machine card: where the run runs, one picker; what the machine offers is in its details. */

import {Banner, Button, Card, Collapsible, Field, KeyValue, Select, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CardProps} from './card-props.js';
import {hardwareLine, type MachineOption, machineFacts, machineLabel} from './view.js';

function MachineDetails(props: CardProps): JSX.Element | null {
  const {context} = props;
  const {machine} = context.form;
  const [open, setOpen] = useState(false);
  const facts = context.facts?.for === machine ? context.facts.value : null;
  if (facts === null) {
    return (
      <Text variant="small" tone="muted">
        Asking {machineLabel(machine)} what it offers.
      </Text>
    );
  }
  if (facts.kind === 'unsupported') {
    return (
      <Text variant="small" tone="muted">
        This machine's VibeSys checkout does not describe the machine; update it to see more.
      </Text>
    );
  }
  return (
    <Collapsible
      label="Details"
      open={open}
      toggle={setOpen}
      {...(open ? {} : {summary: hardwareLine(facts.value)})}
    >
      <KeyValue items={machineFacts(facts.value)} />
    </Collapsible>
  );
}

/** Where the run runs. A machine without a VibeSys checkout says so, and how to fix it, at once. */
export function MachineCard(
  props: CardProps & {
    readonly machines: readonly MachineOption[];
    readonly chooseCheckout: () => void;
  },
): JSX.Element {
  const {context, dispatch} = props;
  const {machine} = context.form;
  const facts = context.facts?.for === machine ? context.facts.value : null;
  const noCheckout = facts?.kind === 'offered' && facts.value.checkout === null;
  return (
    <Card section="Machine">
      <Field label="Run on">
        <Select
          value={machine}
          options={
            props.machines.some(option => option.value === machine)
              ? props.machines
              : [...props.machines, {value: machine, label: machineLabel(machine)}]
          }
          change={next => dispatch({type: 'machine', machine: next})}
        />
      </Field>
      {noCheckout ? (
        <Banner
          tone="warn"
          title={`No VibeSys checkout found on ${machineLabel(machine)}.`}
          actions={<Button text="Choose it in Machines" press={props.chooseCheckout} />}
        />
      ) : (
        <MachineDetails {...props} key={machine} />
      )}
    </Card>
  );
}
