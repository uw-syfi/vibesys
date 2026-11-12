import type {EvalPlace} from '@vibesys/orchestrator-proto';
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
  listingKey,
  type NewRunPage,
  offeredPlaces,
  type PageAction,
  pageReducer,
  pickExample,
  type RepositoryVerdict,
  repositoryVerdict,
  splitPath,
} from '../../state/index.js';
import {CommandLine} from './command-line.js';
import {CatalogStep, withKeptParent} from './new-run/catalog-step.js';
import {MachineStrip} from './new-run/machine-strip.js';
import {OwnStep} from './new-run/own-step.js';
import {
  emptyDraft,
  LEADS,
  missingFrom,
  type StartDraft,
  type StartRunRequest,
  startRunRequest,
} from './start-draft.js';
import {type ModelChoices, modelChoices, NO_CATALOG} from './start-models.js';
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
 * The machine picked, the machine on screen (the picked one once it is read; until then the one
 * before, dimmed), whether the pick is being read, and the key to keep as the one on screen.
 */
function machinesOnScreen(
  hosts: readonly StartHost[],
  chosen: string | null,
  shownKey: string | null,
): {
  readonly picked: StartHost | undefined;
  readonly host: StartHost | undefined;
  readonly reading: boolean;
  readonly nextShown: string | null;
} {
  const picked = hosts.find(candidate => candidate.key === chosen) ?? hosts[0];
  if (picked === undefined) return {picked, host: undefined, reading: false, nextShown: null};
  const reading = picked.catalogue.reading || picked.evalPlaces.reading;
  const nextShown = reading ? (shownKey ?? picked.key) : picked.key;
  const host = hosts.find(candidate => candidate.key === nextShown) ?? picked;
  return {picked, host, reading, nextShown};
}

/** The commands the page sends, as the window gives them. */
interface Commands {
  readonly start: (host: string, request: StartRunRequest) => void;
  readonly checkRepository: (host: string, repository: string) => void;
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
  readonly startFromExample: (
    host: string,
    example: ProvisionExample,
    request: StartRunRequest,
  ) => void;
  readonly forgetProvision: (host: string) => void;
  readonly beginSetup: (host: string, request: StartRunRequest) => void;
  readonly setUpInNewRepository: (
    host: string,
    repository: {readonly name: string; readonly parent: string},
    request: StartRunRequest,
  ) => void;
}

/** What every step reads: the machine on screen, the drafts, and where Start's outcome shows. */
interface Context {
  /** The machine the person picked: where commands go. */
  readonly picked: StartHost | undefined;
  /** The machine on screen. */
  readonly host: StartHost | undefined;
  readonly places: readonly EvalPlace[];
  readonly reading: boolean;
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly choices: ModelChoices;
  readonly sending: boolean;
  readonly status: JSX.Element;
}

type Block = {readonly reason: string; readonly quiet: boolean} | null;

/** The catalogue step with its Start: the example's repository is copied, then the run starts in it. */
function CatalogPane(props: {
  readonly ctx: Context;
  readonly commands: Commands;
  readonly example: ExampleDraft;
  readonly setExample: (draft: ExampleDraft) => void;
}): JSX.Element {
  const {ctx, commands, example} = props;
  const {host, picked, draft, choices} = ctx;
  const entries = host?.catalogue.data?.entries ?? [];
  const target = exampleTarget(entries, ctx.places, withKeptParent(host, example));
  const missing = missingFrom(draft, choices, 'example');
  let block: Block = target.ready ? null : {reason: target.reason, quiet: false};
  if (copying(picked)) block = {reason: 'Copying', quiet: true};
  if (missing !== null) block = {reason: missing, quiet: false};
  if (ctx.sending) block = {reason: 'Sending.', quiet: true};
  return (
    <CatalogStep
      host={host}
      stale={ctx.reading}
      places={ctx.places}
      example={example}
      setExample={props.setExample}
      pick={key => {
        props.setExample(pickExample(example, key));
        if (picked !== undefined) commands.forgetProvision(picked.key);
      }}
      draft={draft}
      set={ctx.set}
      choices={choices}
      block={block}
      start={() => {
        if (!target.ready || picked === undefined) return;
        commands.startFromExample(
          picked.key,
          {example: target.entry.example, name: target.name, parent: target.parent},
          {
            ...startRunRequest(draft, choices),
            task: target.entry.card?.task ?? folderName(target.entry),
            evalPlace: target.place,
          },
        );
      }}
      listDirectory={commands.listDirectory}
      status={ctx.status}
    />
  );
}

/** Why a start in the person's own repository cannot be pressed yet, or null. */
function ownBlock(ctx: Context, verdict: RepositoryVerdict): Block {
  if (ctx.sending) return {reason: 'Sending.', quiet: true};
  const missing = missingFrom(ctx.draft, ctx.choices, 'repository');
  if (missing !== null) return {reason: missing, quiet: false};
  if (verdict._tag === 'Invalid') return {reason: 'Fix the repository.', quiet: true};
  return verdict._tag === 'Checking' ? {reason: 'Checking.', quiet: true} : null;
}

/** The My own step, bound to the commands of the picked machine. */
function OwnPane(props: {
  readonly ctx: Context;
  readonly commands: Commands;
  readonly page: NewRunPage;
  readonly act: (action: PageAction) => void;
}): JSX.Element {
  const {ctx, commands, page} = props;
  const {host, picked, draft, choices} = ctx;
  const folder = repositoriesFolder(host);
  const verdict = repositoryVerdict(draft.repository, picked?.repositoryChecked);
  const on = (send: (machine: StartHost) => void): void => {
    if (picked !== undefined) send(picked);
  };
  return (
    <OwnStep
      host={host}
      stale={ctx.reading}
      places={ctx.places}
      page={page}
      act={props.act}
      draft={draft}
      set={ctx.set}
      choices={choices}
      verdict={verdict}
      repositories={{folder, listing: host?.directories.get(listingKey(`${folder}/`, false))}}
      sending={ctx.sending}
      askAbout={repository => on(machine => commands.checkRepository(machine.key, repository))}
      listFolder={(repository, fresh) => {
        const reached = splitPath(repository);
        if (reached !== undefined)
          on(machine => commands.listDirectory(machine.key, reached.folder, reached.hidden, fresh));
      }}
      start={() => on(machine => commands.start(machine.key, startRunRequest(draft, choices)))}
      setUp={request => on(machine => commands.beginSetup(machine.key, request))}
      setUpNew={() =>
        on(machine =>
          commands.setUpInNewRepository(
            machine.key,
            {name: page.newName.trim(), parent: machine.repositoriesDirectory ?? ''},
            startRunRequest({...draft, repository: ''}, choices),
          ),
        )
      }
      startBlock={ownBlock(ctx, verdict)}
      status={ctx.status}
    />
  );
}

/** The two ways in: the catalogue and the person's own repository. */
function StartStep(props: {readonly act: (action: PageAction) => void}): JSX.Element {
  return (
    <PickCards label="Start from" columns="fit">
      <PickCard
        kicker="Catalog"
        title="Start from an example"
        note="Browse every bundled example by domain, then choose where to evaluate it."
        press={() => props.act({type: 'open', step: 'catalog'})}
      />
      <PickCard
        kicker="My own"
        title="Bring a repository, or describe one"
        note="Start an existing task, turn a repository into one, or create a new repository with guided setup."
        press={() => props.act({type: 'open', step: 'own'})}
      />
    </PickCards>
  );
}

/** Where Start's outcome shows: its line, and the way into the run it made. */
function StartOutcome(props: {
  readonly progress: CommandProgress | undefined;
  readonly retry: () => void;
  readonly open: (runId: string) => void;
}): JSX.Element {
  const {progress} = props;
  return (
    <>
      <CommandLine progress={progress} done="Start" retry={props.retry} />
      {progress?._tag === 'Accepted' && progress.receipt.runId !== '' ? (
        <Button
          text="Open the run"
          variant="secondary"
          size="sm"
          press={() => props.open(progress.receipt.runId)}
        />
      ) : null}
    </>
  );
}

/**
 * The page that starts a run, a step at a time: the machine first, then either the catalogue of
 * examples or the person's own repository. The drafts live here, above the steps, so going back a
 * step loses nothing typed, and a start that was not accepted can be fixed and sent again. While
 * another machine is read the page keeps showing the one before, dimmed, and swaps once.
 */
export function NewRun(
  props: Commands & {
    readonly hosts: readonly StartHost[];
    /** What the last press of Start came to, or undefined before any. */
    readonly progress: CommandProgress | undefined;
    readonly retry: () => void;
    /** Show the run a Start created. */
    readonly openRun: (host: string, runId: string) => void;
    /** Read `host`'s catalogue and eval places. */
    readonly loadNewRun: (host: string) => void;
    /** Back to the run list. */
    readonly openRuns: () => void;
    /** The person asked to edit the machines. */
    readonly editMachines: () => void;
  },
): JSX.Element {
  const [page, act] = useReducer(pageReducer, firstPage);
  const [chosen, setChosen] = useState<string | null>(null);
  const [shownKey, setShownKey] = useState<string | null>(null);
  const [draft, setDraft] = useState<StartDraft>(emptyDraft);
  const [example, setExample] = useState<ExampleDraft>(emptyExampleDraft);
  const {picked, host, reading, nextShown} = machinesOnScreen(props.hosts, chosen, shownKey);
  if (nextShown !== shownKey) setShownKey(nextShown);
  const places = offeredPlaces(host?.evalPlaces.data?.places ?? []);
  const ctx: Context = {
    picked,
    host,
    places,
    reading,
    draft,
    set: change => setDraft({...draft, ...change}),
    choices: picked === undefined ? NO_CATALOG : modelChoices(picked.models, LEADS[draft.lead]),
    sending: props.progress?._tag === 'Sending',
    status: (
      <StartOutcome
        progress={props.progress}
        retry={props.retry}
        open={runId => {
          if (picked !== undefined) props.openRun(picked.key, runId);
        }}
      />
    ),
  };
  const pickMachine = (key: string): void => {
    setChosen(key);
    act({type: 'machine'});
    props.loadNewRun(key);
    props.forgetProvision(key);
    props.checkRepository(key, draft.repository);
    const reached = splitPath(draft.repository);
    if (reached !== undefined) props.listDirectory(key, reached.folder, reached.hidden, true);
  };
  const {title, trail} = trailOf(page.step, step => act({type: 'open', step}), props.openRuns);
  return (
    <Page key="new-run" width="wide" state="ready" title={title} trail={trail}>
      <Stack gap="space-4">
        <MachineStrip
          hosts={props.hosts}
          chosen={picked?.key ?? ''}
          places={places}
          stale={reading}
          failure={host?.catalogue.failure ?? host?.evalPlaces.failure ?? null}
          pick={pickMachine}
          editMachines={props.editMachines}
        />
        {page.step === 'start' ? <StartStep act={act} /> : null}
        {page.step === 'catalog' ? (
          <CatalogPane ctx={ctx} commands={props} example={example} setExample={setExample} />
        ) : null}
        {page.step === 'own' ? <OwnPane ctx={ctx} commands={props} page={page} act={act} /> : null}
      </Stack>
    </Page>
  );
}
