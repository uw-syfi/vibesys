/**
 * What the Run screen shows in place of an ended run while its history is being read: the read's
 * progress (a bar and "n of N events" once the total is known, else the events and bytes so far).
 * The hero, panes and counts wait for the whole history, since a prefix of it would read as a run
 * still going.
 */
import {Button, Card, LoadingBar, Meter, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type HistoryProgress,
  historyLoadingView,
  UNSTARTED_WORDS,
  type UnstartedView,
} from '../../state/index.js';

export function HistoryLoading(props: {readonly progress: HistoryProgress}): JSX.Element {
  const view = historyLoadingView(props.progress);
  return (
    <Card>
      <Stack gap="space-3">
        <Text variant="heading">{view.title}</Text>
        {view.share === null ? (
          <LoadingBar label="Reading run history" />
        ) : (
          <Meter label="Share of the run’s events read" value={view.share} />
        )}
        <Text variant="code" tone="muted">
          {view.detail}
        </Text>
      </Stack>
    </Card>
  );
}

/** The run record or summary is not available yet, so its empty panes cannot be interpreted. */
export function InitialRunLoading(props: {readonly stage: 'locating' | 'summary'}): JSX.Element {
  const locating = props.stage === 'locating';
  return (
    <Card>
      <Stack gap="space-3">
        <Text variant="heading">{locating ? 'Finding the run…' : 'Loading the run summary…'}</Text>
        <LoadingBar label={locating ? 'Finding run' : 'Loading run summary'} />
        <Text variant="code" tone="muted">
          {locating ? 'Checking this machine for the run' : 'Reading its results and ideas'}
        </Text>
      </Stack>
    </Card>
  );
}

/** A run its host holds no record of: said plainly, with no Retry since asking again finds none. */
export function RunGone(props: {readonly words: string}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-3">
        <Text variant="heading">Could not load this run</Text>
        <Text tone="muted">{props.words}</Text>
      </Stack>
    </Card>
  );
}

/** A run the page gave up placing: a status word and the reason in a few words. */
export function RunUnplaced(props: {readonly label: string; readonly words: string}): JSX.Element {
  return (
    <Card>
      <Text variant="heading">{`${props.label} · ${props.words}`}</Text>
    </Card>
  );
}

/**
 * A run that ended before any of its work began, in place of its empty charts and faces: what
 * happened, its whole reason, Run again (the way on) and its log.
 */
export function UnstartedRun(props: {
  readonly view: UnstartedView;
  readonly runAgain: () => void;
  readonly viewLogs: () => void;
}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-3">
        <Text as="h2" variant="heading">
          {props.view.title}
        </Text>
        <Text as="p">{props.view.reason}</Text>
        <Row gap="space-2" wrap>
          <Button
            variant="primary"
            text={UNSTARTED_WORDS.runAgain}
            hint={UNSTARTED_WORDS.runAgainHint}
            press={props.runAgain}
          />
          <Button
            variant="secondary"
            text={UNSTARTED_WORDS.viewLogs}
            hint={UNSTARTED_WORDS.viewLogsHint}
            press={props.viewLogs}
          />
        </Row>
      </Stack>
    </Card>
  );
}

/** The run's history could not be read: why, Retry, and the run's logs, which need no history. */
export function RunLoadError(props: {
  readonly message: string;
  readonly retry: () => void;
  readonly viewLogs: () => void;
}): JSX.Element {
  return (
    <Card>
      <Stack gap="space-3">
        <Text variant="heading">Could not load this run</Text>
        <Text tone="muted">{props.message}</Text>
        <Row gap="space-2" wrap>
          <Button variant="secondary" text="Retry" press={props.retry} />
          <Button variant="secondary" text="View logs" press={props.viewLogs} />
        </Row>
      </Stack>
    </Card>
  );
}
