/**
 * New run: the machine, project and task, agent, and budget on the left; on the right, whether the
 * run is ready to start on that machine, each failed check with its fix, and Start.
 */
import {
  Banner,
  Button,
  Card,
  type CheckItem,
  CheckList,
  Page,
  Split,
  Stack,
  Text,
} from '@vibesys/ui';
import type {JSX, ReactNode} from 'react';
import type {NewRunEvent, NewRunSnapshot} from '../../state/index.js';
import {AgentCard, BudgetCard, type FolderCompletion, MachineCard, ProjectCard} from './inputs.js';
import {type MachineOption, type ReadinessFix, readiness, startBlocked, summary} from './view.js';

type Dispatch = (event: NewRunEvent) => void;

function fixText(fix: ReadinessFix): string {
  switch (fix.kind) {
    case 'copy_out':
      return 'Copy to its own repository';
    case 'create_from_example':
      return 'Create the project';
    case 'choose_checkout':
      return 'Choose it in Machines';
    case 'recheck':
      return 'Check again';
  }
}

function fixPress(fix: ReadinessFix, dispatch: Dispatch, chooseCheckout: () => void): () => void {
  switch (fix.kind) {
    case 'copy_out':
    case 'create_from_example':
      return () => dispatch({type: 'fix', fix});
    case 'choose_checkout':
      return chooseCheckout;
    case 'recheck':
      return () => dispatch({type: 'recheck'});
  }
}

function ReadyCard(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly chooseCheckout: () => void;
}): JSX.Element {
  const {snapshot, dispatch, chooseCheckout} = props;
  const items: CheckItem[] = readiness(snapshot).map(({fix, ...item}) => ({
    ...item,
    ...(fix === undefined
      ? {}
      : {fix: {text: fixText(fix), press: fixPress(fix, dispatch, chooseCheckout)}}),
  }));
  const blocked = startBlocked(snapshot);
  const line = summary(snapshot.context);
  return (
    <Card section="Ready to start">
      <CheckList items={items} />
      {line === null ? null : (
        <Text variant="small" tone="muted">
          {line}
        </Text>
      )}
      <Button
        variant="primary"
        full
        text="Start run"
        press={() => dispatch({type: 'start'})}
        {...(blocked === undefined ? {} : {disabledReason: blocked})}
      />
    </Card>
  );
}

/** The New run screen over its store's snapshot; `hero` is the app's wordmark. */
export function NewRunScreen(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly machines: readonly MachineOption[];
  readonly hero: ReactNode;
  /** Open Machines, where the machine's VibeSys checkout is chosen. */
  readonly chooseCheckout: () => void;
  /** The folder field's path completion. */
  readonly completion: FolderCompletion;
}): JSX.Element {
  const {snapshot, dispatch, chooseCheckout} = props;
  const {context} = snapshot;
  const cardProps = {context, dispatch};
  return (
    <Page width="wide">
      <Stack gap="space-4">
        {/* The wordmark sizes to its container's width, so it is not in the centered stack. */}
        {props.hero}
        <Stack gap="space-1" align="center">
          <Text as="h1" variant="title">
            New run
          </Text>
          <Text tone="muted">Prefilled from your last run on this machine.</Text>
        </Stack>
      </Stack>
      {context.problem === null ? null : (
        <Banner tone="err" title="Something went wrong." body={context.problem} />
      )}
      <Split
        main={
          <Stack gap="space-4">
            <MachineCard {...cardProps} machines={props.machines} chooseCheckout={chooseCheckout} />
            <ProjectCard key={context.form.machine} {...cardProps} completion={props.completion} />
            <AgentCard {...cardProps} />
            <BudgetCard {...cardProps} />
          </Stack>
        }
        aside={
          <ReadyCard snapshot={snapshot} dispatch={dispatch} chooseCheckout={chooseCheckout} />
        }
      />
    </Page>
  );
}
