/**
 * The Machines screen: where runs can go. The saved machines with their status, the selected one's
 * facts and what fixes it, and the Add form. Render is a function of the store's state; every
 * press dispatches an action, except "New run here", which asks the shell to change screens.
 */
import type {HostKey} from '@vibesys/hosts';
import {FAILURE_CAUSE_WORDS, type FailureCause} from '@vibesys/ipc';
import {
  Button,
  Card,
  Chip,
  EmptyState,
  Field,
  Grid,
  KeyValue,
  Page,
  PathInput,
  Row,
  Split,
  Stack,
  Text,
  TextInput,
  Tree,
  TreeRow,
} from '@vibesys/ui';
import type {JSX, KeyboardEvent} from 'react';
import {
  formSuggestions,
  type Machine,
  type MachinesAction,
  type MachinesState,
  machineFacts,
  machineProblem,
  machineStatus,
  machineSummary,
  selectedMachine,
  statusTone,
  statusWord,
} from '../../state/index.js';

type Dispatch = (action: MachinesAction) => void;

const LOCAL: HostKey = 'local';
const CHECKING = 'Checking the checkout';

/**
 * A form problem as its field shows it: a failed connection as its status word (Unreachable) with
 * the raw text on the pointer; any other problem in its own words.
 */
function shownProblem(problem: {readonly text: string; readonly cause?: FailureCause}): {
  readonly error: string;
  readonly errorHint?: string;
} {
  return problem.cause === undefined
    ? {error: problem.text}
    : {error: FAILURE_CAUSE_WORDS[problem.cause], errorHint: problem.text};
}

/** A ref callback with one identity, so it runs as the form mounts and not on every render. */
function focusFirstField(form: HTMLDivElement | null): void {
  form?.querySelector('input')?.focus();
}

/**
 * A form that opens in place: its first field takes the focus as it opens, and Escape closes it
 * (`close`) unless a field in it already used the key (a path's suggestions close first).
 */
function OpenForm(props: {
  readonly close: () => void;
  readonly children: JSX.Element;
}): JSX.Element {
  const keyDown = (event: KeyboardEvent<HTMLDivElement>): void => {
    if (event.key !== 'Escape' || event.defaultPrevented) return;
    event.preventDefault();
    props.close();
  };
  return (
    // biome-ignore lint/a11y/noStaticElementInteractions: the wrapper only hears the Escape that bubbles up from the form's own fields.
    <div ref={focusFirstField} onKeyDown={keyDown}>
      {props.children}
    </div>
  );
}

/** The Add a machine form, open in a card; nothing while it is closed. */
export function AddMachine(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {adding} = props.state;
  const {dispatch} = props;
  if (adding === null) return null;
  const host = adding.host.trim();
  const completions = formSuggestions(adding.path);
  const checking = adding.saving !== null;
  return (
    <Card section="Add a machine">
      <OpenForm close={() => dispatch({type: 'close', form: 'add'})}>
        <Stack>
          <Grid min="field">
            <Field
              label="SSH host"
              hint="As you’d type after ssh; your SSH config and keys apply."
              {...(adding.problem?.field === 'host' ? shownProblem(adding.problem) : {})}
            >
              <TextInput
                value={adding.host}
                placeholder="e.g. host-c"
                change={value => dispatch({type: 'add-host', value})}
              />
            </Field>
            <Field
              label="VibeSys checkout"
              {...(completions.length > 0 && host !== '' ? {help: `Found on ${host}:`} : {})}
              {...(adding.problem?.field === 'checkout' ? shownProblem(adding.problem) : {})}
            >
              <PathInput
                value={adding.path.checkout}
                placeholder="e.g. ~/vibesys"
                completions={completions}
                change={value => dispatch({type: 'checkout', form: 'add', value})}
              />
            </Field>
          </Grid>
          <Row gap="space-2" wrap>
            <Button
              variant="primary"
              text={checking ? 'Checking…' : 'Check and add'}
              {...(checking ? {disabledReason: CHECKING} : {})}
              press={() => dispatch({type: 'submit', form: 'add'})}
            />
            <Button text="Cancel" press={() => dispatch({type: 'close', form: 'add'})} />
          </Row>
        </Stack>
      </OpenForm>
    </Card>
  );
}

function CheckoutForm(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {fixing} = props.state;
  const {dispatch, machine} = props;
  if (fixing === null || fixing.host !== machine.host) return null;
  const checking = fixing.saving !== null;
  return (
    <OpenForm close={() => dispatch({type: 'close', form: 'fix'})}>
      <Stack gap="space-3">
        <Field
          label={`VibeSys checkout on ${machine.label}`}
          {...(fixing.problem === null ? {} : shownProblem(fixing.problem))}
        >
          <PathInput
            value={fixing.path.checkout}
            completions={formSuggestions(fixing.path)}
            change={value => dispatch({type: 'checkout', form: 'fix', value})}
          />
        </Field>
        <Row gap="space-2" wrap>
          <Button
            variant="primary"
            text={checking ? 'Checking…' : 'Check and save'}
            {...(checking ? {disabledReason: CHECKING} : {})}
            press={() => dispatch({type: 'submit', form: 'fix'})}
          />
          <Button text="Cancel" press={() => dispatch({type: 'close', form: 'fix'})} />
        </Row>
      </Stack>
    </OpenForm>
  );
}

/** What fixes a machine that is not ready, when something does: sign in, or point at its checkout. */
function ProblemFix(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, machine, dispatch} = props;
  const status = machineStatus(machine);
  const signingIn = state.pending?.kind === 'sign-in' && state.pending.host === machine.host;
  const fixingHere = state.fixing?.host === machine.host;
  if (status === 'sign-in') {
    return (
      <Row gap="space-2" wrap>
        <Button
          variant="primary"
          text={signingIn ? 'Signing in…' : 'Sign in'}
          {...(signingIn ? {disabledReason: 'Waiting for the host to accept the sign-in.'} : {})}
          press={() => dispatch({type: 'sign-in', host: machine.host})}
        />
      </Row>
    );
  }
  if (status !== 'checkout-not-found') return null;
  return fixingHere ? (
    <CheckoutForm state={state} machine={machine} dispatch={dispatch} />
  ) : (
    <Row gap="space-2" wrap>
      <Button
        variant="primary"
        text="Choose the checkout"
        press={() => dispatch({type: 'open-fix', host: machine.host})}
      />
    </Row>
  );
}

/**
 * What the screen says when no machine is selected, which (by the store's invariants) is when none
 * is listed: while they are read, that they are being read, not that there are none; while a form
 * is open, nothing, since the form says what to do.
 */
function NoMachine(props: {readonly state: MachinesState}): JSX.Element | null {
  const {state} = props;
  if (state.adding !== null) return null;
  return (
    <Card>
      {state.listing !== null ? (
        <EmptyState title="Reading the machines" />
      ) : (
        <EmptyState title="No machine yet" />
      )}
    </Card>
  );
}

function Detail(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
  readonly newRun: (host: HostKey) => void;
}): JSX.Element {
  const {state, machine, dispatch, newRun} = props;
  const status = machineStatus(machine);
  const problem = machineProblem(machine);
  const removing = state.pending?.kind === 'remove' && state.pending.host === machine.host;
  const savingHere = state.fixing?.host === machine.host && state.fixing.saving !== null;
  const changeDisabled =
    state.pending === null ? null : 'Wait for the current machine action to finish.';
  const removeDisabled = removing
    ? 'Removing this machine.'
    : savingHere
      ? 'Wait for the checkout check to finish.'
      : changeDisabled;
  return (
    <Card>
      <Stack>
        <Row gap="space-3" align="center" wrap>
          <Stack grow>
            <Text as="h2" variant="heading">
              {machine.label}
            </Text>
          </Stack>
          <Chip
            label={statusWord(status)}
            tone={statusTone(status)}
            {...(problem === null ? {} : {hint: problem})}
          />
        </Row>
        <ProblemFix state={state} machine={machine} dispatch={dispatch} />
        {status === 'checkout-not-found' ? null : (
          <CheckoutForm state={state} machine={machine} dispatch={dispatch} />
        )}
        <KeyValue items={machineFacts(machine)} />
        {state.actionProblem === null ? null : (
          <Text tone="err" variant="small">
            {state.actionProblem}
          </Text>
        )}
        <Row gap="space-2" wrap>
          <Button text="New run here" press={() => newRun(machine.host)} />
          <Button
            variant="quiet"
            text="Change checkout"
            {...(changeDisabled === null ? {} : {disabledReason: changeDisabled})}
            press={() => dispatch({type: 'open-fix', host: machine.host})}
          />
          {machine.host === LOCAL ? null : (
            <Button
              variant="quiet"
              text={removing ? 'Removing…' : 'Remove'}
              {...(removeDisabled === null ? {} : {disabledReason: removeDisabled})}
              press={() => dispatch({type: 'remove', host: machine.host})}
            />
          )}
        </Row>
      </Stack>
    </Card>
  );
}

function MachineList(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {state, dispatch} = props;
  return (
    <Card flush>
      <Tree label="Machines" {...(state.selected === null ? {} : {selected: state.selected})}>
        {state.machines.map(machine => {
          const status = machineStatus(machine);
          const word = statusWord(status);
          return (
            <TreeRow
              key={machine.host}
              id={machine.host}
              depth={0}
              title={machine.label}
              subtitle={machineSummary(machine)}
              state={{tone: statusTone(status), label: word}}
              chips={[{label: word, tone: statusTone(status)}]}
              select={() => dispatch({type: 'select', host: machine.host})}
            />
          );
        })}
      </Tree>
    </Card>
  );
}

/** The Machines screen over its store's `state`; `newRun` opens New run on a machine. */
export function MachinesScreen(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
  readonly newRun: (host: HostKey) => void;
  readonly back: () => void;
  /** Settings supplies the surrounding section navigation and page header. */
  readonly embedded?: boolean;
}): JSX.Element {
  const {state, dispatch, newRun} = props;
  const machine = selectedMachine(state);
  const header = (
    <Row gap="space-3" align="end" wrap>
      {props.embedded === true ? null : (
        <Button
          variant="quiet"
          size="sm"
          icon="chevron-left"
          label="Back to Settings"
          press={props.back}
        />
      )}
      <Stack gap="space-1" grow>
        <Text variant="section">Machines</Text>
        <Text
          as="h1"
          variant="title"
          title="Each machine runs VibeSys from a checkout you already have there."
        >
          Where runs can go
        </Text>
      </Stack>
      <Button variant="primary" text="Add a machine" press={() => dispatch({type: 'open-add'})} />
    </Row>
  );
  const content = (
    <Stack>
      <AddMachine state={state} dispatch={dispatch} />
      {machine === null ? (
        <NoMachine state={state} />
      ) : (
        <Split
          asideFirst
          aside={<MachineList state={state} dispatch={dispatch} />}
          main={<Detail state={state} machine={machine} dispatch={dispatch} newRun={newRun} />}
        />
      )}
    </Stack>
  );
  if (props.embedded === true)
    return (
      <Stack>
        {header}
        {content}
      </Stack>
    );
  return <Page header={header}>{content}</Page>;
}
