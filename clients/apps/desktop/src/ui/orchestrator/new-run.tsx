import {Button, Dialog, Field, SegmentedControl, Select, Stack} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {ProvisionExample} from '../../core/index.js';
import {
  type CommandProgress,
  type ExampleDraft,
  emptyExampleDraft,
  exampleTarget,
  folderName,
  pickExample,
} from '../../state/index.js';
import {ExamplePicker} from './new-run/example-picker.js';
import {placeLabel} from './new-run/words.js';
import {emptyDraft, type StartDraft, type StartRunRequest} from './start-draft.js';
import {type StartHost, StartRunForm} from './start-run-form.js';

/** The host is making the repository of an example. */
const copying = (host: StartHost | undefined): boolean => host?.provision._tag === 'Working';

/** What a new run starts from: an example of the host's catalogue, or a repository of the person's own. */
type Source = 'catalogue' | 'repository';

/**
 * The way to start a run: a button that opens New run in a dialog, from the host's catalogue or from
 * a repository the person names. The drafts are kept here, above the dialog, so putting the dialog
 * away (Escape, a press outside) loses nothing the person typed, and a start that was not accepted
 * can be fixed and sent again.
 */
export function NewRun(props: {
  readonly hosts: readonly StartHost[];
  /** What the last press of Start came to, or undefined before any. */
  readonly progress: CommandProgress | undefined;
  readonly start: (host: string, request: StartRunRequest) => void;
  readonly checkRepository: (host: string, repository: string) => void;
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
  readonly retry: () => void;
  /** Show the run a Start created. */
  readonly openRun: (host: string, runId: string) => void;
  /** Read `host`'s catalogue and eval places. */
  readonly loadNewRun: (host: string) => void;
  /** Make the repository of an example on `host` and start in it; `request` names no repository yet. */
  readonly startFromExample: (
    host: string,
    example: ProvisionExample,
    request: StartRunRequest,
  ) => void;
  /** Forget what the host said of the last copy. */
  readonly forgetProvision: (host: string) => void;
  /** Begin a setup chat in the repository named, which makes its task. */
  readonly beginSetup: (host: string, request: StartRunRequest) => void;
}): JSX.Element {
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<StartDraft>(emptyDraft);
  const [chosen, setChosen] = useState<string | null>(null);
  const [source, setSource] = useState<Source>('catalogue');
  const [example, setExample] = useState<ExampleDraft>(emptyExampleDraft);
  const host = props.hosts.find(candidate => candidate.key === chosen) ?? props.hosts[0];
  const entries = host?.catalogue.data?.entries ?? [];
  const places = host?.evalPlaces.data?.places ?? [];
  const target = exampleTarget(entries, places, example);
  const pick = (picked: string): void => {
    setExample(pickExample(example, picked));
    if (host !== undefined) props.forgetProvision(host.key);
  };
  const startExample = (hostKey: string, request: StartRunRequest): void => {
    if (!target.ready) return;
    props.startFromExample(
      hostKey,
      {example: target.entry.example, name: target.name, parent: target.parent},
      {
        ...request,
        task: target.entry.card?.task ?? folderName(target.entry),
        evalPlace: target.place,
      },
    );
  };
  return (
    <>
      <Button
        text="New run"
        icon="plus"
        variant="primary"
        press={() => {
          setOpen(true);
          if (host !== undefined) props.loadNewRun(host.key);
        }}
      />
      <Dialog open={open} title="New run" dismiss={() => setOpen(false)}>
        <Stack gap="space-3">
          <SegmentedControl
            label="Start from"
            value={source}
            options={[
              {value: 'catalogue', label: 'Example'},
              {value: 'repository', label: 'My repository'},
            ]}
            change={setSource}
          />
          <StartRunForm
            hosts={props.hosts}
            draft={draft}
            setDraft={setDraft}
            chosen={chosen}
            choose={key => {
              setChosen(key);
              props.loadNewRun(key);
              props.forgetProvision(key);
            }}
            progress={props.progress}
            start={props.start}
            checkRepository={props.checkRepository}
            listDirectory={props.listDirectory}
            retry={props.retry}
            openRun={(hostKey, runId) => {
              setOpen(false);
              props.openRun(hostKey, runId);
            }}
            {...(source === 'catalogue'
              ? {
                  example: {
                    picker: (
                      <ExamplePicker
                        host={host}
                        draft={example}
                        setDraft={setExample}
                        pick={pick}
                        listDirectory={props.listDirectory}
                      />
                    ),
                    blocked: copying(host) ? 'Copying' : target.ready ? null : target.reason,
                    start: startExample,
                  },
                }
              : {
                  setUp: props.beginSetup,
                  placeField: (
                    <Field label="Evals on">
                      <Select
                        value={draft.evalPlace}
                        options={[
                          {value: '', label: 'Automatic'},
                          ...places.map(place => ({
                            value: place.name,
                            label: placeLabel(place, null),
                          })),
                        ]}
                        change={evalPlace => setDraft({...draft, evalPlace})}
                        mono
                      />
                    </Field>
                  ),
                })}
          />
        </Stack>
      </Dialog>
    </>
  );
}
