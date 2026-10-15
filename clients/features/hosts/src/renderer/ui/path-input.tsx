/**
 * A host path field that completes as the user types: a `Combobox` whose options are the
 * directories the host listed for the typed directory (`state/completion.ts`). It renders the
 * state it is given and reports events; it performs no I/O.
 */

import type {HostKey} from '@vibesys/hosts';
import {Combobox} from '@vibesys/ui/legacy';
import type {InputHTMLAttributes, JSX} from 'react';
import {
  type CompletionAction,
  type CompletionField,
  type CompletionState,
  suggestions,
} from '../../state/completion.js';

export type PathInputProps = {
  readonly field: CompletionField;
  readonly host: HostKey;
  readonly value: string;
  readonly completion: CompletionState;
  readonly complete: (action: CompletionAction) => void;
  readonly change: (value: string) => void;
  /** Paths to offer besides the completions (recent projects). */
  readonly offers?: readonly string[];
} & Omit<InputHTMLAttributes<HTMLInputElement>, 'value' | 'onChange' | 'list'>;

export function PathInput(props: PathInputProps): JSX.Element {
  const {field, host, value, completion, complete, change, offers = [], ...input} = props;
  return (
    <Combobox
      {...input}
      listId={`${field}-completions`}
      value={value}
      options={[...suggestions(completion, field), ...offers]}
      onFocus={() => complete({type: 'input', field, host, value})}
      change={next => {
        change(next);
        complete({type: 'input', field, host, value: next});
      }}
    />
  );
}
