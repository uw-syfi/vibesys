/**
 * New run as a guided form: one question to a page, in the order a run is decided. The steps sit
 * in a list beside the page (each shows its answer and takes you back to it) that, in a narrower
 * pane, is one line ("4 of 8 · Hardware") which opens the list. Every answer is kept, so leaving
 * New run and coming back finds the page where it was left. The first page opens with the
 * wordmark above its question; the others do not.
 */
import type {HostKey} from '@vibesys/hosts';
import {Button, Page, Row, SectionLayout, SidebarItem, Stack, Text, Wordmark} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  examplesStatus,
  isDone,
  type NewRunContext,
  type NewRunEvent,
  type NewRunSnapshot,
  type StepId,
} from '../../state/index.js';
import {AgentPage} from './agent-page.js';
import {
  DomainPage,
  ExamplePage,
  ExamplesFailed,
  HardwarePage,
  ModelPage,
  StartPage,
  StructurePage,
  VariantPage,
  WorkloadPage,
} from './example-pages.js';
import {GoalPage} from './goal-page.js';
import {type FolderCompletion, ProjectPage, TaskPage} from './project-pages.js';
import {Nav, Question} from './question.js';
import {ProblemBanner, ReviewPage, StartButton} from './review-page.js';
import {progressLine, type StepView, stepViews} from './steps.js';
import {entriesIn, type MachineOption} from './view.js';
import {WherePage} from './where-page.js';
import {VIBESYS_WORDMARK} from './wordmark-art.js';

type Dispatch = (event: NewRunEvent) => void;

/** The question of each page. */
const COPY: Readonly<Record<StepId, {readonly title: string}>> = {
  start: {
    title: 'What do you want to optimize?',
  },
  domain: {title: 'Which domain?'},
  model: {
    title: 'Which model do you want to serve?',
  },
  hardware: {
    title: 'Which hardware are you optimizing for?',
  },
  structure: {title: 'Which structure?'},
  workload: {
    title: 'Which workload?',
  },
  example: {title: 'Which example?'},
  variant: {title: 'Which task?'},
  project: {
    title: 'Which project?',
  },
  task: {title: 'Which task?'},
  goal: {
    title: 'This is what the agent will work toward',
  },
  where: {
    title: 'Where does the agent work, and where are results measured?',
  },
  agent: {
    title: 'Which agent does the work?',
  },
  review: {title: 'Ready to start?'},
};

interface ScreenProps {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly machines: readonly MachineOption[];
  /** Open Machines, where the machine's VibeSys checkout is chosen. */
  readonly chooseCheckout: () => void;
  /** Open Settings on the clusters of `machine`, where a place to measure is set up. */
  readonly setUp: (machine: HostKey) => void;
  /** The folder field's path completion. */
  readonly completion: FolderCompletion;
}

/** The steps that ask about the machine's examples. */
const EXAMPLE_STEPS: ReadonlySet<StepId> = new Set([
  'domain',
  'model',
  'hardware',
  'structure',
  'workload',
  'example',
  'variant',
]);

/** Whether the machine's examples failed to list, so an example step has nothing to show. */
function examplesFailed(context: NewRunContext, at: StepId | null): boolean {
  return (
    examplesStatus(context.examples, context.form.machine) === 'failed' &&
    (at === null || EXAMPLE_STEPS.has(at))
  );
}

/** The page of the step shown. */
function StepBody(props: ScreenProps & {readonly at: StepId}): JSX.Element {
  const {snapshot, dispatch} = props;
  const cardProps = {context: snapshot.context, dispatch};
  if (examplesFailed(snapshot.context, props.at)) {
    return <ExamplesFailed {...cardProps} machines={props.machines} />;
  }
  switch (props.at) {
    case 'start':
      return <StartPage {...cardProps} />;
    case 'domain':
      return <DomainPage {...cardProps} />;
    case 'model':
      return <ModelPage {...cardProps} />;
    case 'hardware':
      return <HardwarePage {...cardProps} />;
    case 'structure':
      return <StructurePage {...cardProps} />;
    case 'workload':
      return <WorkloadPage {...cardProps} />;
    case 'example':
      return <ExamplePage {...cardProps} />;
    case 'variant':
      return <VariantPage {...cardProps} />;
    case 'project':
      return <ProjectPage {...cardProps} machines={props.machines} completion={props.completion} />;
    case 'task':
      return <TaskPage {...cardProps} />;
    case 'goal':
      return <GoalPage {...cardProps} />;
    case 'where':
      return (
        <WherePage
          {...cardProps}
          machines={props.machines}
          chooseCheckout={props.chooseCheckout}
          setUp={() => props.setUp(snapshot.context.form.machine)}
        />
      );
    case 'agent':
      return <AgentPage {...cardProps} />;
    case 'review':
      return (
        <ReviewPage snapshot={snapshot} dispatch={dispatch} chooseCheckout={props.chooseCheckout} />
      );
  }
}

/** Back, and the way on: Continue, Looks right on the goal, Start run on Review. */
function StepNav(props: ScreenProps & {readonly at: StepId; readonly first: boolean}): JSX.Element {
  const {snapshot, dispatch, at} = props;
  const {context} = snapshot;
  if (at === 'review') {
    return (
      <Row justify="between" align="start" wrap>
        <Button text="Back" press={() => dispatch({type: 'back'})} />
        <StartButton snapshot={snapshot} dispatch={dispatch} />
      </Row>
    );
  }
  const done = isDone(at, context.answers, entriesIn(context), {
    agent: context.form.agent !== null,
  });
  return (
    <Nav
      {...(props.first ? {} : {back: () => dispatch({type: 'back'})})}
      next={{
        text: at === 'goal' ? 'Looks right' : 'Continue',
        press: () =>
          at === 'goal'
            ? dispatch({type: 'answer', change: {kind: 'goal'}, advance: true})
            : dispatch({type: 'next'}),
        // The goal is answered by accepting it, which is what its button does.
        blocked: done || at === 'goal' ? undefined : '',
      }}
    />
  );
}

/** The steps beside the page: each shows its answer and goes back to it; one line when narrow. */
function Steps(props: {
  readonly views: readonly StepView[];
  readonly at: StepId | null;
  readonly started: boolean;
  readonly dispatch: Dispatch;
}): JSX.Element {
  const {dispatch} = props;
  return (
    <>
      {props.views.map((view, index) => (
        <SidebarItem
          key={view.id}
          label={view.title}
          {...(view.answer === null || view.id === props.at ? {} : {detail: view.answer})}
          marker={{
            text: String(index + 1),
            state: view.id === props.at ? 'current' : view.answer === null ? 'muted' : 'neutral',
            label: `Step ${index + 1}, ${view.answer === null ? 'not answered yet' : 'answered'}`,
          }}
          active={view.id === props.at}
          // The step shown is not a way to anywhere.
          disabled={!view.reached || view.id === props.at}
          press={() => dispatch({type: 'go', step: view.id})}
        />
      ))}
      {props.started ? (
        <Button
          variant="quiet"
          size="sm"
          text="Start over"
          press={() => dispatch({type: 'reset'})}
        />
      ) : null}
    </>
  );
}

/** The New run screen over its store's snapshot. */
export function NewRunScreen(props: ScreenProps): JSX.Element {
  const {snapshot, dispatch} = props;
  const {context} = snapshot;
  const views = stepViews(context);
  // A kept form whose steps wait on the machine's examples shows no page until they are listed.
  const at = views.find(view => view.id === context.at)?.id ?? null;
  const index = views.findIndex(view => view.id === at);
  const copy = at === null ? null : COPY[at];
  return (
    <Page
      width="wide"
      {...(at === null ? {} : {footer: <StepNav {...props} at={at} first={index === 0} />})}
    >
      <SectionLayout
        label="New run steps"
        summary={
          at === null
            ? examplesFailed(context, at)
              ? 'Examples failed'
              : 'Listing the examples'
            : progressLine(views, at)
        }
        sections={
          <Steps
            views={views}
            at={at}
            started={context.answers.from !== null}
            dispatch={dispatch}
          />
        }
      >
        <Stack gap="space-5">
          {at === 'start' ? <Wordmark art={VIBESYS_WORDMARK} size="compact" /> : null}
          <ProblemBanner context={context} dispatch={dispatch} />
          {at === null || copy === null ? (
            examplesFailed(context, at) ? (
              <ExamplesFailed context={context} dispatch={dispatch} machines={props.machines} />
            ) : (
              <Text tone="muted">Listing</Text>
            )
          ) : (
            <Question
              kicker={`New run \u00b7 step ${index + 1} of ${views.length}`}
              title={copy.title}
            >
              <StepBody {...props} at={at} />
            </Question>
          )}
        </Stack>
      </SectionLayout>
    </Page>
  );
}
