import {Button, Chip, KeyValue, MiddleText, Pane, Reserve, Row, Stack, Text} from '@vibesys/ui';
import type {JSX} from 'react';
import type {CheckRow, SetupFacts, StartOutcome} from '../../../state/index.js';
import {runTitle} from '../fleet-words.js';
import {receiptRefusalWord} from '../words.js';
import {
  AS_OF_LAST_READ,
  baselineWords,
  CHECK_LABEL,
  CHECK_WORD,
  objectiveWords,
  spendWords,
  timeWords,
} from './setup-words.js';

const NOTHING = '—';
const BLANK = ' ';

/**
 * One check: its name, its status in a slot as wide as the longest word so the word never moves its
 * line, and the host's short detail under it. The detail line is always there (blank when the host
 * said nothing), so a detail arriving moves nothing. A stale read keeps the rows and quiets them.
 */
function CheckLine(props: {readonly row: CheckRow; readonly dim: boolean}): JSX.Element {
  const {row, dim} = props;
  const word = CHECK_WORD[row.state];
  return (
    <Stack gap="space-1">
      <Row justify="between" gap="space-3">
        <Text variant="body-strong" tone={dim ? 'muted' : 'default'}>
          {CHECK_LABEL[row.id]}
        </Text>
        <Chip label={word.label} tone={dim ? 'neutral' : word.tone} dot even />
      </Row>
      <Text variant="small" tone="muted">
        {row.detail === '' ? BLANK : <MiddleText text={row.detail} />}
      </Text>
    </Stack>
  );
}

/** The line under Start: the checks the host says have not passed, or how the press went; always one line. */
function outcomeLine(
  outcome: StartOutcome,
): {readonly text: string; readonly tone: 'err' | 'muted'} | null {
  switch (outcome.kind) {
    case 'incomplete':
      return {
        text: `Not passed: ${outcome.unmet.map(id => CHECK_LABEL[id]).join(', ')}`,
        tone: 'err',
      };
    case 'refused':
      return {text: receiptRefusalWord(outcome.receipt), tone: 'err'};
    case 'unsent':
      return {text: 'Not confirmed', tone: 'err'};
    default:
      return null;
  }
}

/** Why Start cannot be pressed, as a word, or null when it can. */
function startReason(facts: SetupFacts, outcome: StartOutcome, starting: boolean): string | null {
  if (starting) return 'Starting';
  if (outcome.kind === 'sending') return 'Sending';
  return facts.startable ? null : 'Checks pending';
}

/**
 * The task as it forms, beside the chat: its name, the card's facts, the six checks, the baseline,
 * what setup has cost (apart from the run's limits) and Start. Every row is there from the first
 * frame, with a dash or a blank where a fact is not known yet, so nothing is added or moved as the
 * lead works; when the host could not read just now the same facts stay, quieter, under one status word.
 */
export function DraftPanel(props: {
  readonly facts: SetupFacts;
  readonly outcome: StartOutcome;
  /** Start was taken and the run has not left setup yet: the lead is finishing its turn. */
  readonly starting: boolean;
  readonly start: () => void;
  readonly retry: () => void;
}): JSX.Element {
  const {facts, outcome} = props;
  const dim = facts.readFailure !== null;
  const card = facts.draft.kind === 'card' ? facts.draft.card : null;
  const title = card === null ? facts.task : runTitle(card, facts.task);
  const line = outcomeLine(outcome);
  const reason = startReason(facts, outcome, props.starting);
  return (
    <Pane
      kicker="Task draft"
      title={title === '' ? 'Untitled' : title}
      width="outline"
      fixed
      meta={
        dim ? (
          <Chip label={AS_OF_LAST_READ} tone="warn" dot hint={facts.readFailure ?? ''} />
        ) : (
          <Reserve>
            <Chip label={AS_OF_LAST_READ} tone="warn" dot />
          </Reserve>
        )
      }
    >
      <KeyValue
        items={[
          {key: 'Objective', value: card === null ? NOTHING : objectiveWords(card) || NOTHING},
          {key: 'Hardware', value: card?.hardwareName || NOTHING},
          {
            key: 'Baseline',
            value: facts.baseline === null ? NOTHING : baselineWords(facts.baseline),
          },
          {key: 'Setup spend', value: spendWords(facts.spentUsd)},
          {key: 'Setup time', value: timeWords(facts.elapsedSeconds)},
        ]}
      />
      <Stack gap="space-2">
        {facts.checks.map(row => (
          <CheckLine key={row.id} row={row} dim={dim} />
        ))}
      </Stack>
      <Row gap="space-3">
        <Button
          text="Start"
          label="Start run"
          variant="primary"
          press={props.start}
          {...(reason === null ? {} : {disabledReason: reason, quietReason: true})}
        />
        <Text variant="small" tone={line?.tone ?? 'muted'}>
          {line?.text ?? BLANK}
        </Text>
        {outcome.kind === 'unsent' ? (
          <Button text="Try again" variant="secondary" size="sm" press={props.retry} />
        ) : null}
      </Row>
    </Pane>
  );
}
