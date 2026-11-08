import type {EvalPlace} from '@vibesys/orchestrator-proto';
import {
  Button,
  Field,
  PathInput,
  PickCard,
  PickCards,
  Row,
  Select,
  Stack,
  Stale,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  completionsOf,
  type FolderListing,
  listingKey,
  type NewRunPage,
  offeredPlaces,
  type OwnCase,
  type PageAction,
  type PathCompletion,
  type RepositoryVerdict,
  splitPath,
} from '../../../state/index.js';
import {SetUpButton} from '../setup/set-up-button.js';
import type {StartDraft, StartRunRequest} from '../start-draft.js';
import type {ModelChoices} from '../start-models.js';
import {LimitFields, MoreOptions, type StartHost} from '../start-run-form.js';
import {placeLabel, provisionLine} from './words.js';

const CASES: readonly {
  readonly value: OwnCase;
  readonly title: string;
  readonly note: string;
}[] = [
  {
    value: 'tasks',
    title: 'A repository with a task',
    note: 'It has .vibesys/tasks/. Pick one and start.',
  },
  {
    value: 'repo',
    title: 'A repository without a task',
    note: 'Describe the goal; a setup chat writes the task.',
  },
  {value: 'none', title: 'Nothing yet', note: 'We create the repository, then the setup chat.'},
];

/** What the host said of the path typed: its refusal, or the tasks it holds; one line that keeps its room. */
function checkWords(verdict: RepositoryVerdict): {readonly help?: string; readonly error?: string} {
  switch (verdict._tag) {
    case 'Empty':
      return {};
    case 'Checking':
      return {help: 'Checking'};
    case 'Invalid':
      return {error: verdict.reason};
    case 'Valid':
      return {
        help:
          verdict.tasks.length === 0
            ? 'Git repository, no tasks'
            : `Git repository, ${verdict.tasks.length} ${verdict.tasks.length === 1 ? 'task' : 'tasks'}`,
      };
  }
}

/**
 * The completions the case offers: a folder the host lists as holding tasks for the first case, a
 * repository with none for the second. A folder that is neither is left out, as the host's listing
 * of it says; the person can still type a path.
 */
function completionsFor(
  own: OwnCase | null,
  completions: readonly PathCompletion[],
): readonly PathCompletion[] {
  if (own === 'tasks') return completions.filter(each => each.detail === 'tasks');
  if (own === 'repo') return completions.filter(each => each.detail === 'git');
  return completions;
}

/** The repository path of a case that names an existing repository, completed from the host's folders. */
function RepositoryPath(props: {
  readonly host: StartHost | undefined;
  readonly own: OwnCase | null;
  readonly draft: StartDraft;
  readonly verdict: RepositoryVerdict;
  readonly change: (repository: string) => void;
  readonly enter: () => void;
}): JSX.Element {
  const {host, draft} = props;
  const split = splitPath(draft.repository);
  const listed =
    split === undefined
      ? []
      : completionsOf(split, host?.directories.get(listingKey(split.folder, split.hidden)));
  return (
    <Field label={`Repository on ${host?.label ?? ''}`} reserveLine {...checkWords(props.verdict)}>
      <PathInput
        value={draft.repository}
        change={props.change}
        completions={completionsFor(props.own, listed).map(({value, detail}) => ({
          value,
          ...(detail === undefined ? {} : {detail}),
        }))}
        tabCompletes={split !== undefined && split.prefix !== ''}
        focus={props.enter}
        placeholder={props.own === 'tasks' ? 'Pick or type a path' : '~/code/my-server'}
      />
    </Field>
  );
}

/** Whether `name` is already a folder of the host's Repositories folder, as its listing says. */
const taken = (listing: FolderListing | undefined, name: string): boolean =>
  listing?._tag === 'Listed' && listing.entries.some(entry => entry.name === name);

/**
 * The My own step: what the person brings, and under it what that case needs: a repository with a
 * task is picked and started; one without is set up in a chat; with nothing yet the repository is
 * made first. The machine's data is dimmed in place while another machine is read.
 */
export function OwnStep(props: {
  readonly host: StartHost | undefined;
  readonly stale: boolean;
  readonly places: readonly EvalPlace[];
  readonly page: NewRunPage;
  readonly act: (action: PageAction) => void;
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly choices: ModelChoices;
  readonly verdict: RepositoryVerdict;
  /** The folder new repositories are made in (the machine's kept one, else the host's own), with its listing. */
  readonly repositories: {readonly folder: string; readonly listing: FolderListing | undefined};
  readonly sending: boolean;
  readonly askAbout: (repository: string) => void;
  readonly listFolder: (repository: string, fresh: boolean) => void;
  readonly start: () => void;
  readonly setUp: (request: StartRunRequest) => void;
  readonly setUpNew: () => void;
  readonly startBlock: {readonly reason: string; readonly quiet: boolean} | null;
  readonly request: StartRunRequest;
  readonly status: JSX.Element | null;
}): JSX.Element {
  const {host, page, draft, verdict} = props;
  const names = verdict._tag === 'Valid' ? verdict.tasks : [];
  const clash = taken(props.repositories.listing, page.newName.trim());
  const copying = host?.provision._tag === 'Working';
  const line = provisionLine(host?.provision ?? {_tag: 'Idle'});
  const newBlock =
    page.newName.trim() === ''
      ? 'Name the repository.'
      : clash
        ? 'Name taken'
        : props.sending || copying
          ? 'Sending.'
          : null;
  return (
    <Stack gap="space-4">
      <PickCards label="What you bring" columns="fit">
        {CASES.map(each => (
          <PickCard
            key={each.value}
            title={each.title}
            note={each.note}
            selected={page.own === each.value}
            press={() => props.act({type: 'own', own: each.value})}
          />
        ))}
      </PickCards>
      <Stale stale={props.stale}>
        {page.own === 'tasks' || page.own === 'repo' ? (
          <Stack gap="space-4">
            <RepositoryPath
              host={host}
              own={page.own}
              draft={draft}
              verdict={verdict}
              change={repository => {
                props.set({repository, task: ''});
                props.act({type: 'own-task', task: null});
                props.askAbout(repository);
                props.listFolder(repository, false);
              }}
              enter={() => props.listFolder(draft.repository, true)}
            />
            {page.own === 'tasks' ? (
              <Stack gap="space-4">
                <PickCards label="Tasks" columns="fit">
                  {names.map(name => (
                    <PickCard
                      key={name}
                      title={name}
                      id={`.vibesys/tasks/${name}`}
                      selected={page.ownTask === name}
                      press={() => {
                        props.act({type: 'own-task', task: name});
                        props.set({task: name});
                      }}
                    />
                  ))}
                </PickCards>
                {page.ownTask === null ? null : (
                  <Stack gap="space-3">
                    <Field label="Evals on">
                      <Select
                        value={draft.evalPlace}
                        options={[
                          {value: '', label: 'Automatic'},
                          ...offeredPlaces(props.places).map(place => ({
                            value: place.name,
                            label: placeLabel(place, null),
                          })),
                        ]}
                        change={evalPlace => props.set({evalPlace})}
                        mono
                      />
                    </Field>
                    <LimitFields draft={draft} set={props.set} />
                    <Row>
                      <Button
                        text="Start run"
                        label="Start run"
                        variant="primary"
                        press={() => {
                          if (props.startBlock === null) props.start();
                        }}
                        {...(props.startBlock === null
                          ? {}
                          : {
                              disabledReason: props.startBlock.reason,
                              quietReason: props.startBlock.quiet,
                            })}
                      />
                    </Row>
                    <MoreOptions draft={draft} set={props.set} choices={props.choices} />
                  </Stack>
                )}
              </Stack>
            ) : (
              <Row>
                <SetUpButton
                  draft={draft}
                  choices={props.choices}
                  verdict={verdict}
                  sending={props.sending}
                  setUp={props.setUp}
                />
              </Row>
            )}
          </Stack>
        ) : page.own === 'none' ? (
          <Stack gap="space-4">
            <Field
              label="Name"
              reserveLine
              {...(clash
                ? {error: 'Name taken'}
                : line?.failed === true
                  ? {error: line.text, ...(line.title === undefined ? {} : {errorHint: line.title})}
                  : {
                      help: `Created in ${props.repositories.folder === '' ? "the host's folder" : props.repositories.folder}`,
                    })}
            >
              <TextInput
                value={page.newName}
                change={name => props.act({type: 'new-name', name})}
                placeholder="ring-buffer"
                mono
              />
            </Field>
            <Row>
              <Button
                text="Set up a task"
                variant="secondary"
                press={() => {
                  if (newBlock === null) props.setUpNew();
                }}
                {...(newBlock === null ? {} : {disabledReason: newBlock, quietReason: true})}
              />
            </Row>
          </Stack>
        ) : null}
      </Stale>
      {props.status}
    </Stack>
  );
}
