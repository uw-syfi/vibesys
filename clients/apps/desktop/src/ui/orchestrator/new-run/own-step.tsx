import type {EvalPlace} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  Field,
  Measure,
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
  type OwnCase,
  offeredPlaces,
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
  readonly kicker: string;
  readonly title: string;
  readonly note: string;
  readonly details: readonly string[];
}[] = [
  {
    value: 'tasks',
    kicker: 'Ready to run',
    title: 'A repository with a task',
    note: 'Pick a task already defined under .vibesys/tasks/, review its limits, and start immediately.',
    details: ['Existing repository', 'Existing task'],
  },
  {
    value: 'repo',
    kicker: 'Guided setup',
    title: 'A repository without a task',
    note: 'Choose an existing Git repository, describe the outcome, and use a setup chat to create the task.',
    details: ['Existing repository', 'Create task'],
  },
  {
    value: 'none',
    kicker: 'Start fresh',
    title: 'Nothing yet',
    note: 'Name a new repository. VibeSys creates it first, then guides you through defining the task.',
    details: ['Create repository', 'Create task'],
  },
];

/** What the host said of the path typed: its refusal, or the tasks it holds; one line that keeps its room. */
function checkWords(verdict: RepositoryVerdict): {
  readonly help?: string;
  readonly helpTone?: 'ok';
  readonly error?: string;
} {
  switch (verdict._tag) {
    case 'Empty':
      return {};
    case 'Checking':
      return {help: 'Checking'};
    case 'Invalid':
      return {error: verdict.reason};
    case 'Valid':
      return {
        helpTone: 'ok',
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
  /** The Repositories folder, whose listing the empty field offers. */
  readonly folder: string;
  readonly change: (repository: string) => void;
  readonly enter: () => void;
}): JSX.Element {
  const {host, draft} = props;
  const split = splitPath(draft.repository.trim() === '' ? `${props.folder}/` : draft.repository);
  const listed =
    split === undefined
      ? []
      : completionsOf(split, host?.directories.get(listingKey(split.folder, split.hidden)));
  return (
    <Measure>
      {' '}
      <Field
        label={`Repository on ${host?.label ?? ''}`}
        reserveLine
        {...checkWords(props.verdict)}
      >
        <PathInput
          value={draft.repository}
          change={props.change}
          completions={(props.verdict._tag === 'Valid'
            ? []
            : completionsFor(props.own, listed)
          ).map(({value, detail}) => ({
            value,
            ...(detail === undefined ? {} : {detail}),
          }))}
          tabCompletes={
            split !== undefined && split.prefix !== '' && draft.repository.trim() !== ''
          }
          focus={props.enter}
          placeholder={props.own === 'tasks' ? 'Pick or type a path' : '~/code/my-server'}
        />
      </Field>
    </Measure>
  );
}

/** Whether `name` is already a folder of the host's Repositories folder, as its listing says. */
const taken = (listing: FolderListing | undefined, name: string): boolean =>
  listing?._tag === 'Listed' && listing.entries.some(entry => entry.name === name);

/** What the own step's cases read and send. */
interface OwnProps {
  readonly host: StartHost | undefined;
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
}

/** The task picked in a repository, with where it evaluates, its limits, and Start. */
function PickedTask(props: OwnProps): JSX.Element {
  const {draft, startBlock} = props;
  return (
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
            if (startBlock === null) props.start();
          }}
          {...(startBlock === null
            ? {}
            : {disabledReason: startBlock.reason, quietReason: startBlock.quiet})}
        />
      </Row>
      <MoreOptions draft={draft} set={props.set} choices={props.choices} />
    </Stack>
  );
}

/** A repository that is already there: its path, then its tasks to start, or a setup chat to make one. */
function ExistingRepository(props: OwnProps & {readonly own: 'tasks' | 'repo'}): JSX.Element {
  const {host, page, draft, verdict} = props;
  const names = verdict._tag === 'Valid' ? verdict.tasks : [];
  return (
    <Stack gap="space-4">
      <RepositoryPath
        host={host}
        own={props.own}
        draft={draft}
        verdict={verdict}
        folder={props.repositories.folder}
        change={repository => {
          props.set({repository, task: ''});
          props.act({type: 'own-task', task: null});
          props.askAbout(repository);
          props.listFolder(repository, false);
        }}
        enter={() => props.listFolder(draft.repository, true)}
      />
      {props.own === 'repo' ? (
        <Row>
          <SetUpButton
            draft={draft}
            choices={props.choices}
            verdict={verdict}
            sending={props.sending}
            setUp={props.setUp}
          />
        </Row>
      ) : (
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
            <Card>
              <PickedTask {...props} />
            </Card>
          )}
        </Stack>
      )}
    </Stack>
  );
}

/** The Name field's line: the clash, the host's refusal of the last copy, or where the repository goes. */
function nameLine(
  clash: boolean,
  line: ReturnType<typeof provisionLine>,
  folder: string,
): {
  readonly help?: string;
  readonly helpTone?: 'ok' | 'warn';
  readonly error?: string;
  readonly errorHint?: string;
} {
  if (clash) return {help: 'Name taken', helpTone: 'warn'};
  if (line?.failed === true) {
    return {error: line.text, ...(line.title === undefined ? {} : {errorHint: line.title})};
  }
  return {help: `Created in ${folder === '' ? "the host's folder" : folder}`};
}

/** Why the setup of a new repository cannot begin yet, or null. */
function newBlock(name: string, clash: boolean, busy: boolean): string | null {
  if (name === '') return 'Name the repository.';
  if (clash) return 'Name taken';
  return busy ? 'Sending.' : null;
}

/** Nothing yet: the repository's name, then a setup chat in the repository made with it. */
function NewRepository(props: OwnProps): JSX.Element {
  const {host, page} = props;
  const name = page.newName.trim();
  const clash = taken(props.repositories.listing, name);
  const block = newBlock(name, clash, props.sending || host?.provision._tag === 'Working');
  return (
    <Stack gap="space-4">
      <Measure>
        {' '}
        <Field
          label="Name"
          reserveLine
          {...nameLine(
            clash,
            provisionLine(host?.provision ?? {_tag: 'Idle'}),
            props.repositories.folder,
          )}
        >
          <TextInput
            value={page.newName}
            change={newName => props.act({type: 'new-name', name: newName})}
            placeholder="ring-buffer"
            mono
          />
        </Field>
      </Measure>
      <Row>
        <Button
          text="Set up a task"
          variant="secondary"
          press={() => {
            if (block === null) props.setUpNew();
          }}
          {...(block === null ? {} : {disabledReason: block, quietReason: true})}
        />
      </Row>
    </Stack>
  );
}

/**
 * The My own step: what the person brings, and under it what that case needs: a repository with a
 * task is picked and started; one without is set up in a chat; with nothing yet the repository is
 * made first. The machine's data is dimmed in place while another machine is read.
 */
export function OwnStep(
  props: OwnProps & {readonly stale: boolean; readonly status: JSX.Element | null},
): JSX.Element {
  const {page} = props;
  return (
    <Stack gap="space-4">
      <PickCards label="What you bring" columns="fit">
        {CASES.map(each => (
          <PickCard
            key={each.value}
            kicker={each.kicker}
            title={each.title}
            note={each.note}
            chips={each.details.map(detail => <Chip key={detail} label={detail} />)}
            selected={page.own === each.value}
            press={() => {
              // A case starts with an empty path, as the prototype's does: the one typed was for another case.
              props.act({type: 'own', own: each.value});
              props.set({repository: '', task: ''});
            }}
          />
        ))}
      </PickCards>
      <Stale stale={props.stale}>
        {page.own === 'tasks' || page.own === 'repo' ? (
          <ExistingRepository {...props} own={page.own} />
        ) : null}
        {page.own === 'none' ? <NewRepository {...props} /> : null}
      </Stale>
      {props.status}
    </Stack>
  );
}
