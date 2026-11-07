import {Button, Card, Field, PathInput, Row, Stack} from '@vibesys/ui';
import type {JSX} from 'react';
import type {AddMachineState} from '../../../state/index.js';

/**
 * The one input that adds a machine: an ssh config alias (matches are offered as it is typed) or a
 * pasted ssh command. The arrows move through the matches, Enter adds the highlighted match or what
 * was typed, Escape closes, and a refusal is read under the input. The focus lands in the input.
 */
export function AddMachine(props: {
  readonly state: AddMachineState;
  readonly type: (input: string) => void;
  /** The person picked a match: add it. */
  readonly pick: (alias: string) => void;
  readonly submit: () => void;
  readonly close: () => void;
}): JSX.Element {
  const {state} = props;
  return (
    <Card>
      <Row gap="space-3" align="end" wrap>
        <Stack gap="space-2">
          <Field label="Machine" {...(state.error === null ? {} : {error: state.error})}>
            <PathInput
              takeFocus
              value={state.input}
              change={props.type}
              completions={state.matches.map(value => ({value}))}
              placeholder="host or ssh command"
              pick={completion => props.pick(completion.value)}
              submit={props.submit}
              escape={props.close}
            />
          </Field>
        </Stack>
        <Button
          variant="primary"
          text="Add"
          press={props.submit}
          {...(state.busy || state.input.trim() === ''
            ? {disabledReason: 'Type a host or paste an ssh command', quietReason: true}
            : {})}
        />
        <Button variant="quiet" text="Close" press={props.close} />
      </Row>
    </Card>
  );
}
