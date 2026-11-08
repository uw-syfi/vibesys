import {Button} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RepositoryVerdict} from '../../../state/index.js';
import {
  missingFrom,
  type StartDraft,
  type StartRunRequest,
  startRunRequest,
} from '../start-draft.js';
import type {ModelChoices} from '../start-models.js';

/** Why a setup cannot begin yet, as a word, or null when it can: it needs a repository and a lead, no task. */
function setUpBlock(
  sending: boolean,
  draft: StartDraft,
  choices: ModelChoices,
  verdict: RepositoryVerdict,
): string | null {
  if (sending) return 'Sending.';
  if (draft.repository.trim() === '') return 'Name the repository to optimize.';
  if (verdict._tag === 'Invalid') return 'Fix the repository.';
  if (verdict._tag === 'Checking') return 'Checking.';
  return missingFrom(draft, choices, 'example');
}

/**
 * Begins a run in its setup phase in the repository named, with the lead chosen: a chat that makes the
 * task, for a repository that has none yet (or one the person wants another of). It needs no task name.
 */
export function SetUpButton(props: {
  readonly draft: StartDraft;
  readonly choices: ModelChoices;
  readonly verdict: RepositoryVerdict;
  readonly sending: boolean;
  readonly setUp: (request: StartRunRequest) => void;
}): JSX.Element {
  const block = setUpBlock(props.sending, props.draft, props.choices, props.verdict);
  return (
    <Button
      text="Set up a task"
      variant="secondary"
      press={() => block === null && props.setUp(startRunRequest(props.draft, props.choices))}
      {...(block === null ? {} : {disabledReason: block, quietReason: true})}
    />
  );
}
