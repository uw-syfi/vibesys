import {Button, Dialog} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CommandProgress} from '../../state/index.js';
import {emptyDraft, type StartDraft, type StartRunRequest} from './start-draft.js';
import {type StartHost, StartRunForm} from './start-run-form.js';

/**
 * The way to start a run: a button that opens the Start run form in a dialog. The draft is kept
 * here, above the dialog, so putting the dialog away (Escape, a press outside) loses nothing the
 * person typed, and a start that was not accepted can be fixed and sent again.
 */
export function NewRun(props: {
  readonly hosts: readonly StartHost[];
  /** What the last press of Start came to, or undefined before any. */
  readonly progress: CommandProgress | undefined;
  readonly start: (host: string, request: StartRunRequest) => void;
  readonly checkRepository: (host: string, repository: string) => void;
  readonly retry: () => void;
  /** Show the run a Start created. */
  readonly openRun: (host: string, runId: string) => void;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<StartDraft>(emptyDraft);
  const [chosen, setChosen] = useState<string | null>(null);
  return (
    <>
      <Button text="New run" icon="plus" variant="primary" press={() => setOpen(true)} />
      <Dialog open={open} title="New run" dismiss={() => setOpen(false)}>
        <StartRunForm
          hosts={props.hosts}
          draft={draft}
          setDraft={setDraft}
          chosen={chosen}
          choose={setChosen}
          progress={props.progress}
          start={props.start}
          checkRepository={props.checkRepository}
          retry={props.retry}
          openRun={(host, runId) => {
            setOpen(false);
            props.openRun(host, runId);
          }}
        />
      </Dialog>
    </>
  );
}
