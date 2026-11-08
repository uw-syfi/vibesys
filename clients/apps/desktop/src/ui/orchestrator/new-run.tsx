import {Button, type Crumb, Page, PickCard, PickCards, Stack} from '@vibesys/ui';
import {type JSX, useReducer, useState} from 'react';
import type {ProvisionExample} from '../../core/index.js';
import {
  type CommandProgress,
  type ExampleDraft,
  emptyExampleDraft,
  exampleTarget,
  firstPage,
  folderName,
  type FolderEntryFacts,
  listingKey,
  machineFacts,
  offeredPlaces,
  pageReducer,
  pickExample,
  repositoryVerdict,
  splitPath,
} from '../../state/index.js';
import {CommandLine} from './command-line.js';
import {CatalogStep, withKeptParent} from './new-run/catalog-step.js';
import {MachineStrip} from './new-run/machine-strip.js';
import {OwnStep} from './new-run/own-step.js';
import {
  emptyDraft,
  missingFrom,
  type StartDraft,
  type StartRunRequest,
  startRunRequest,
} from './start-draft.js';
import {modelChoices, NO_CATALOG} from './start-models.js';
import {LEADS} from './start-draft.js';
import type {StartHost} from './start-run-form.js';

/** The host is making the repository of an example. */
const copying = (host: StartHost | undefined): boolean => host?.provision._tag === 'Working';

const TITLE = 'Runs';

/** The folder new repositories go in, with no trailing slash: the machine's kept one, else the host's own. */
function repositoriesFolder(host: StartHost | undefined): string {
  if (host === undefined) return '';
  const kept = host.repositoriesDirectory ?? '';
  const folder = kept === '' ? (host.catalogue.data?.repositoriesDirectory ?? '') : kept;
  return folder.replace(/\/+$/, '');
}

/** What the host listed of a folder, or null while it has not. */
function folderFacts(
  host: StartHost | undefined,
  folder: string,
): readonly FolderEntryFacts[] | null {
  if (host === undefined || folder === '') return null;
  const listing = host.directories.get(listingKey(`${folder}/`, false));
  return listing?._tag === 'Listed' ? listing.entries : null;
}

/** The title of each step, and the trail down to it. */
function trailOf(
  step: 'start' | 'catalog' | 'own',
  go: (step: 'start') => void,
  runs: () => void,
): {
  readonly title: string;
  readonly trail: readonly Crumb[];
} {
  const base: Crumb = {id: 'runs', label: TITLE, press: runs};
  switch (step) {
    case 'start':
      return {title: 'New run', trail: [base, {id: 'new-run', label: 'New run'}]};
    case 'catalog':
      return {
        title: 'Start from an example',
        trail: [
          base,
          {id: 'new-run', label: 'New run', press: () => go('start')},
          {id: 'catalog', label: 'Catalog'},
        ],
      };
    case 'own':
      return {
        title: 'Bring your own',
        trail: [
          base,
          {id: 'new-run', label: 'New run', press: () => go('start')},
          {id: 'own', label: 'My own'},
        ],
      };
  }
}

/**
 * The page that starts a run, a step at a time: the machine first, then either the catalogue of
 * examples or the person's own repository. The drafts live here, above the steps, so going back a
 * step loses nothing typed, and a start that was not accepted can be fixed and sent again. While
 * another machine is read the page keeps showing the one before, dimmed, and swaps once.
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
  /** Make an empty repository, then begin a setup chat in it. */
  readonly setUpInNewRepository: (
    host: string,
    repository: {readonly name: string; readonly parent: string},
    request: StartRunRequest,
  ) => void;
  /** Back to the run list. */
  readonly openRuns: () => void;
  /** The person asked to edit the machines. */
  readonly editMachines: () => void;
}): JSX.Element {
  const [page, act] = useReducer(pageReducer, firstPage);
  const [chosen, setChosen] = useState<string | null>(null);
  const [shownKey, setShownKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<StartDraft>(emptyDraft);
  const [example, setExample] = useState<ExampleDraft>(emptyExampleDraft);
  const picked = props.hosts.find(candidate => candidate.key === chosen) ?? props.hosts[0];
  // The machine on screen is the picked one once it is read; until then the one before, dimmed.
  const reading = picked !== undefined && (picked.catalogue.reading || picked.evalPlaces.reading);
  const nextShown = picked === undefined ? null : reading ? (shownKey ?? picked.key) : picked.key;
  if (nextShown !== shownKey) setShownKey(nextShown);
  const host = props.hosts.find(candidate => candidate.key === (shownKey ?? picked?.key)) ?? picked;
  const places = offeredPlaces(host?.evalPlaces.data?.places ?? []);
  const entries = host?.catalogue.data?.entries ?? [];
  const folder = repositoriesFolder(host);
  const facts = machineFacts(
    host?.evalPlaces.data?.places ?? null,
    host?.catalogue.data?.entries ?? null,
    folderFacts(host, folder),
  );
  const set = (change: Partial<StartDraft>): void => setDraft({...draft, ...change});
  const choices =
    picked === undefined ? NO_CATALOG : modelChoices(picked.models, LEADS[draft.lead]);
  const sending = props.progress?._tag === 'Sending';
  const status = (
    <>
      <CommandLine progress={props.progress} done="Start" retry={props.retry} />
      {props.progress?._tag === 'Accepted' &&
      props.progress.receipt.runId !== '' &&
      picked !== undefined ? (
        <OpenRun
          open={() => {
            if (props.progress?._tag === 'Accepted') {
              props.openRun(picked.key, props.progress.receipt.runId);
            }
          }}
        />
      ) : null}
    </>
  );
  const pickMachine = (key: string): void => {
    setChosen(key);
    act({type: 'machine'});
    props.loadNewRun(key);
    props.forgetProvision(key);
    props.checkRepository(key, draft.repository);
    const reached = splitPath(draft.repository);
    if (reached !== undefined) props.listDirectory(key, reached.folder, reached.hidden, true);
  };
  // The catalogue step: Start copies the example's repository, then starts in it.
  const target = exampleTarget(entries, places, withKeptParent(host, example));
  const exampleBlock = ((): {readonly reason: string; readonly quiet: boolean} | null => {
    if (sending) return {reason: 'Sending.', quiet: true};
    const missing = missingFrom(draft, choices, 'example');
    if (missing !== null) return {reason: missing, quiet: false};
    if (copying(picked)) return {reason: 'Copying', quiet: true};
    return target.ready ? null : {reason: target.reason, quiet: false};
  })();
  const startExample = (): void => {
    if (!target.ready || picked === undefined) return;
    props.startFromExample(
      picked.key,
      {example: target.entry.example, name: target.name, parent: target.parent},
      {
        ...startRunRequest(draft, choices),
        task: target.entry.card?.task ?? folderName(target.entry),
        evalPlace: target.place,
      },
    );
  };
  // The own step.
  const verdict = repositoryVerdict(draft.repository, picked?.repositoryChecked);
  const ownBlock = ((): {readonly reason: string; readonly quiet: boolean} | null => {
    if (sending) return {reason: 'Sending.', quiet: true};
    const missing = missingFrom(draft, choices, 'repository');
    if (missing !== null) return {reason: missing, quiet: false};
    if (verdict._tag === 'Invalid') return {reason: 'Fix the repository.', quiet: true};
    return verdict._tag === 'Checking' ? {reason: 'Checking.', quiet: true} : null;
  })();
  const {title, trail} = trailOf(page.step, step => act({type: 'open', step}), props.openRuns);
  return (
    <Page key="new-run" width="wide" state="ready" title={title} trail={trail}>
      <Stack gap="space-4">
        <MachineStrip
          hosts={props.hosts}
          chosen={picked?.key ?? ''}
          places={places}
          facts={facts}
          stale={reading}
          failure={host?.catalogue.failure ?? host?.evalPlaces.failure ?? null}
          pick={pickMachine}
          editMachines={props.editMachines}
        />
        {page.step === 'start' ? (
          <PickCards label="Start from" columns="fit">
            <PickCard
              kicker="Catalog"
              title="Start from an example"
              note={`${entries.length} tasks across ${new Set(entries.map(entry => entry.card?.domain)).size} domains, ready to run.`}
              press={() => act({type: 'open', step: 'catalog'})}
            />
            <PickCard
              kicker="My own"
              title="Bring a repository, or describe one"
              note="Use your task folder, or set one up in a chat."
              press={() => act({type: 'open', step: 'own'})}
            />
          </PickCards>
        ) : page.step === 'catalog' ? (
          <CatalogStep
            host={host}
            stale={reading}
            places={places}
            example={example}
            setExample={setExample}
            pick={key => {
              setExample(pickExample(example, key));
              if (picked !== undefined) props.forgetProvision(picked.key);
            }}
            draft={draft}
            set={set}
            choices={choices}
            target={target}
            block={exampleBlock}
            start={startExample}
            listDirectory={props.listDirectory}
            status={status}
          />
        ) : (
          <OwnStep
            host={host}
            stale={reading}
            places={places}
            page={page}
            act={act}
            draft={draft}
            set={set}
            choices={choices}
            verdict={verdict}
            repositories={{
              folder,
              listing: host?.directories.get(listingKey(`${folder}/`, false)),
            }}
            sending={sending}
            askAbout={repository => {
              if (picked !== undefined) props.checkRepository(picked.key, repository);
            }}
            listFolder={(repository, fresh) => {
              const reached = splitPath(repository);
              if (picked !== undefined && reached !== undefined)
                props.listDirectory(picked.key, reached.folder, reached.hidden, fresh);
            }}
            start={() => {
              if (picked !== undefined) props.start(picked.key, startRunRequest(draft, choices));
            }}
            setUp={request => {
              if (picked !== undefined) props.beginSetup(picked.key, request);
            }}
            setUpNew={() => {
              if (picked !== undefined)
                props.setUpInNewRepository(
                  picked.key,
                  {name: page.newName.trim(), parent: picked.repositoriesDirectory ?? ''},
                  startRunRequest({...draft, repository: ''}, choices),
                );
            }}
            startBlock={ownBlock}
            request={startRunRequest(draft, choices)}
            status={status}
          />
        )}
      </Stack>
    </Page>
  );
}

/** The way into the run a Start made, which is where the person goes next. */
function OpenRun(props: {readonly open: () => void}): JSX.Element {
  return <Button text="Open the run" variant="secondary" size="sm" press={props.open} />;
}
