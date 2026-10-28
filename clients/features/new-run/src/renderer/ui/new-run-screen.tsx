/**
 * New run in two steps: pick what to optimize, then review and start, with every other answer a row
 * filled from the defaults and changed in place. The steps sit in a list beside the page (each
 * shows what it holds and takes you back to it) that, in a narrower pane, is one line ("1 of 2 ·
 * Choose what to optimize") which opens the list. Every answer is kept, so leaving New run and
 * coming back finds the page where it was left.
 */
import {Button, Page, Row, SectionLayout, SidebarItem, Stack, Text} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {
  isDone,
  type NewRunContext,
  type NewRunEvent,
  type NewRunSnapshot,
  reviewWords,
  type StepId,
} from '../../state/index.js';
import {PickPage} from './pick-page.js';
import type {FolderCompletion} from './project-pages.js';
import {Question} from './question.js';
import {ProblemBanner, type ReviewLinks, ReviewNav, ReviewPage} from './review-page.js';
import {pickedWords, progressLine, type StepView, stepViews} from './steps.js';
import {entriesIn, type MachineOption, readiness} from './view.js';

type Dispatch = (event: NewRunEvent) => void;
type Marker = NonNullable<Parameters<typeof SidebarItem>[0]['marker']>;

/** The question of each page. */
const TITLES: Readonly<Record<StepId, string>> = {
  pick: 'What do you want to optimize?',
  review: 'Review and start',
};

type ScreenProps = ReviewLinks & {
  readonly snapshot: NewRunSnapshot;
  readonly dispatch: Dispatch;
  readonly machines: readonly MachineOption[];
  /** The folder field's path completion. */
  readonly completion: FolderCompletion;
};

/** What the pick holds, and the way on to Review. */
function PickNav(props: ScreenProps): JSX.Element {
  const {snapshot, dispatch} = props;
  const {context} = snapshot;
  const done = isDone('pick', context.answers, entriesIn(context));
  const picked = done ? pickedWords(context) : null;
  return (
    // Top-aligned: the reason a blocked Continue gives hangs below the row, not inside its height.
    <Row justify="between" align="start" wrap>
      {picked === null ? (
        <span />
      ) : (
        <Row gap="space-2">
          <Text tone="muted">Selected</Text>
          <Text variant="code">{picked}</Text>
        </Row>
      )}
      <Button
        variant="primary"
        text="Continue"
        press={() => dispatch({type: 'next'})}
        {...(done ? {} : {disabledReason: 'Choose a task first.', quietReason: true})}
      />
    </Row>
  );
}

/** A step's marker: its number, or a tick once the pick is made and the form has moved on. */
function markerOf(view: StepView, index: number, at: StepId): Marker {
  const done = view.id !== at && view.id === 'pick' && view.answer !== null;
  const where = done ? 'done' : view.reached ? 'open' : 'not reached yet';
  return {
    text: done ? '✓' : String(index + 1),
    state: view.id === at ? 'current' : view.reached ? 'neutral' : 'muted',
    label: `Step ${index + 1}, ${where}`,
  };
}

/** The steps beside the page: each shows what it holds and goes back to it; one line when narrow. */
function Steps(props: {
  readonly views: readonly StepView[];
  readonly at: StepId;
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
          {...(view.answer === null ? {} : {detail: view.answer})}
          marker={markerOf(view, index, props.at)}
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
/**
 * Whether Review has been opened since the form was started: until it has, the step list says
 * what Review holds, not how many of its checks fail (the screen's own memory, as a scroll is).
 */
function useReviewed(context: NewRunContext): boolean {
  const [reviewed, setReviewed] = useState(context.at === 'review');
  // Kept in step with the form while rendering, as React's "information from previous renders".
  if (context.at === 'review' && !reviewed) setReviewed(true);
  if (context.answers.from === null && context.at === 'pick' && reviewed) setReviewed(false);
  return reviewed;
}

export function NewRunScreen(props: ScreenProps): JSX.Element {
  const {snapshot, dispatch} = props;
  const {context} = snapshot;
  const failed = readiness(snapshot).filter(item => item.state === 'fail').length;
  const views = stepViews(context, reviewWords(failed, useReviewed(context)));
  const {at} = context;
  const index = views.findIndex(view => view.id === at);
  return (
    <Page
      width="wide"
      footer={
        at === 'pick' ? (
          <PickNav {...props} />
        ) : (
          <ReviewNav snapshot={snapshot} dispatch={dispatch} />
        )
      }
    >
      <SectionLayout
        label="New run steps"
        summary={progressLine(views, at)}
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
          <ProblemBanner context={context} dispatch={dispatch} />
          <Question kicker={`New run · step ${index + 1} of ${views.length}`} title={TITLES[at]}>
            {at === 'pick' ? (
              <PickPage
                context={context}
                dispatch={dispatch}
                machines={props.machines}
                completion={props.completion}
              />
            ) : (
              <ReviewPage {...props} />
            )}
          </Question>
        </Stack>
      </SectionLayout>
    </Page>
  );
}
