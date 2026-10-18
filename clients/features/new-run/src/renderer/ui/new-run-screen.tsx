/**
 * New run: the essentials in the order a run is decided (the machine, the project and task, the
 * agent, the budget) and, below them, one closed disclosure for everything else. The wordmark and
 * title stay in view above the form, which scrolls on its own; "Ready to start" is pinned below it
 * as one line (ready, with a count, or what failed, open with its fix) and the Start button, so
 * Start is never out of the window.
 */
import {
  ActionPanel,
  Banner,
  Button,
  type CheckItem,
  CheckList,
  type CheckState,
  Page,
  Stack,
  Text,
  useSideActionLayout,
} from '@vibesys/ui';
import {type JSX, type ReactNode, useState} from 'react';
import type {NewRunEvent, NewRunSnapshot} from '../../state/index.js';
import {AgentCard} from './agent-card.js';
import {BudgetCard} from './budget-card.js';
import {MachineCard} from './machine-card.js';
import {MoreOptions} from './more-options.js';
import {type FolderCompletion, ProjectCard} from './project-card.js';
import {
  type MachineOption,
  type ReadinessFix,
  readiness,
  readinessStatus,
  startBlocked,
  summary,
} from './view.js';

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
      return () => dispatch({type: 'refresh'});
  }
}

const RANK: Readonly<Record<CheckState, number>> = {fail: 0, warn: 1, pending: 2, ok: 3};

/**
 * "Ready to start": one line while all is well, the failed checks and their fixes open as soon as
 * one fails. Beside the form the checks are open to start with; under it (a narrow window) they
 * start closed, so the form and Start run stay in view. The user's own opening or closing holds
 * until the line's kind changes (ready, failing).
 */
function ReadyPanel(props: {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly chooseCheckout: () => void;
}): JSX.Element {
  const {snapshot, dispatch, chooseCheckout} = props;
  const checks = readiness(snapshot);
  // What needs the reader comes first: a failed check and its fix are never below the fold.
  const items: CheckItem[] = [...checks]
    .sort((left, right) => RANK[left.state] - RANK[right.state])
    .map(({fix, ...item}) => ({
      ...item,
      ...(fix === undefined
        ? {}
        : {fix: {text: fixText(fix), press: fixPress(fix, dispatch, chooseCheckout)}}),
    }));
  const status = readinessStatus(checks);
  const [chosen, setChosen] = useState<{readonly for: boolean; readonly open: boolean} | null>(
    null,
  );
  // A choice is for the kind of line it was made on: a check that starts failing opens it again.
  // Unless the line fails, the checks open by themselves only where they have a column of their own.
  const sideLayout = useSideActionLayout();
  const open = chosen?.for === status.open ? chosen.open : status.open || sideLayout;
  const blocked = startBlocked(snapshot);
  const line = summary(snapshot.context);
  return (
    <ActionPanel
      label="Ready to start"
      status={{state: status.state, text: status.text}}
      open={open}
      toggle={next => setChosen({for: status.open, open: next})}
      details={<CheckList items={items} />}
      {...(line === null ? {} : {summary: line})}
      action={
        <Button
          variant="primary"
          text="Start run"
          press={() => dispatch({type: 'start'})}
          {...(blocked === undefined ? {} : {disabledReason: blocked})}
        />
      }
    />
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
  const offers = context.harnesses?.for === context.form.machine ? context.harnesses.list : null;
  return (
    <Page
      width="wide"
      sideAction
      header={
        <>
          {/* The wordmark sizes to its container's width, so it is not in the centered stack. */}
          {props.hero}
          <Stack gap="space-1" align="center">
            <Text as="h1" variant="title">
              New run
            </Text>
            <Text tone="muted">Prefilled from your last run on this machine.</Text>
          </Stack>
          {context.problem === null ? null : (
            <Banner tone="err" title="Something went wrong." body={context.problem} />
          )}
        </>
      }
      footer={
        <ReadyPanel snapshot={snapshot} dispatch={dispatch} chooseCheckout={chooseCheckout} />
      }
    >
      <MachineCard {...cardProps} machines={props.machines} chooseCheckout={chooseCheckout} />
      <ProjectCard key={context.form.machine} {...cardProps} completion={props.completion} />
      <AgentCard {...cardProps} />
      <BudgetCard {...cardProps} />
      <MoreOptions {...cardProps} offers={offers ?? []} />
    </Page>
  );
}
