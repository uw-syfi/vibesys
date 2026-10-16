/**
 * The Machines screen: where runs can go. The saved machines with their status, the selected one's
 * facts and what fixes it, and the Add form. Render is a function of the store's state; every
 * press dispatches an action, except "New run here", which asks the shell to change screens.
 */
import type {HostKey} from '@vibesys/hosts';
import {
  Banner,
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
import type {JSX} from 'react';
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
const CHECKING = 'The app is starting VibeSys from this checkout to check it.';

function AddMachine(props: {
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
      <Stack>
        <Grid min="field">
          <Field label="SSH host" help="As you’d type after ssh; your SSH config and keys apply.">
            <TextInput
              value={adding.host}
              placeholder="chelan5"
              change={value => dispatch({type: 'add-host', value})}
            />
          </Field>
          <Field
            label="VibeSys checkout"
            {...(completions.length > 0 && host !== '' ? {help: `Found on ${host}:`} : {})}
            {...(adding.problem === null ? {} : {error: adding.problem})}
          >
            <PathInput
              value={adding.path.checkout}
              placeholder="~/vibesys"
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
    <Stack gap="space-3">
      <Field
        label={`VibeSys checkout on ${machine.label}`}
        {...(fixing.problem === null ? {} : {error: fixing.problem})}
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
  );
}

function Problem(props: {
  readonly state: MachinesState;
  readonly machine: Machine;
  readonly dispatch: Dispatch;
}): JSX.Element | null {
  const {state, machine, dispatch} = props;
  const problem = machineProblem(machine);
  if (problem === null) return null;
  const status = machineStatus(machine);
  const signingIn = state.pending?.kind === 'sign-in' && state.pending.host === machine.host;
  const fixingHere = state.fixing?.host === machine.host;
  const actions =
    status === 'sign-in' ? (
      <Button
        variant="primary"
        text={signingIn ? 'Signing in…' : 'Sign in'}
        {...(signingIn ? {disabledReason: 'Waiting for the host to accept the sign-in.'} : {})}
        press={() => dispatch({type: 'sign-in', host: machine.host})}
      />
    ) : status !== 'checkout-not-found' ? undefined : fixingHere ? (
      <CheckoutForm state={state} machine={machine} dispatch={dispatch} />
    ) : (
      <Button
        variant="primary"
        text="Choose the checkout"
        press={() => dispatch({type: 'open-fix', host: machine.host})}
      />
    );
  return (
    <Banner
      tone="warn"
      title={statusWord(status)}
      body={problem}
      {...(actions === undefined ? {} : {actions})}
    />
  );
}

function Detail(props: {
  readonly state: MachinesState;
  readonly dispatch: Dispatch;
  readonly newRun: (host: HostKey) => void;
}): JSX.Element {
  const {state, dispatch, newRun} = props;
  const machine = selectedMachine(state);
  if (machine === null) {
    return (
      <Card>
        <EmptyState
          title="No machine yet"
          body="Add the SSH host where your VibeSys checkout is."
        />
      </Card>
    );
  }
  const status = machineStatus(machine);
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
          <Chip label={statusWord(status)} tone={statusTone(status)} />
        </Row>
        <Problem state={state} machine={machine} dispatch={dispatch} />
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
}): JSX.Element {
  const {state, dispatch, newRun} = props;
  return (
    <Page>
      <Stack>
        <Row gap="space-3" align="end" wrap>
          <Stack gap="space-1" grow>
            <Text variant="section">Machines</Text>
            <Text as="h1" variant="title">
              Where runs can go
            </Text>
            <Text tone="muted">
              Each machine runs VibeSys from a checkout you already have there.
            </Text>
          </Stack>
          <Button
            variant="primary"
            text="Add a machine"
            press={() => dispatch({type: 'open-add'})}
          />
        </Row>
        <AddMachine state={state} dispatch={dispatch} />
        {state.listProblem === null ? null : (
          <Banner tone="err" title="The machines could not be read" body={state.listProblem} />
        )}
        <Split
          asideFirst
          aside={<MachineList state={state} dispatch={dispatch} />}
          main={<Detail state={state} dispatch={dispatch} newRun={newRun} />}
        />
      </Stack>
    </Page>
  );
}
