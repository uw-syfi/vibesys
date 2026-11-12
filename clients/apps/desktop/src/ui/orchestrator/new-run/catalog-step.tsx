import {wordsOfId} from '@vibesys/display-names';
import type {CatalogueEntry, CatalogueProblem, EvalPlace} from '@vibesys/orchestrator-proto';
import {
  Button,
  Card,
  Chip,
  EmptyState,
  Field,
  PathInput,
  PickCard,
  PickCards,
  Row,
  SearchBox,
  Select,
  Split,
  Stack,
  Stale,
  Text,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  type CatalogueItem,
  catalogueItemDomain,
  catalogueItemExample,
  catalogueItemKey,
  catalogueItemNeedsClone,
  catalogueItems,
  catalogueProblemWords,
  completionsOf,
  type ExampleDraft,
  fitsHere,
  folderName,
  groupCatalogueItems,
  listingKey,
  nameOfExample,
  objectiveChip,
  placeChoice,
  placesFor,
  splitPath,
  variantOf,
  visibleCatalogueItems,
} from '../../../state/index.js';
import {acceleratorName, runTitle} from '../fleet-words.js';
import type {StartDraft} from '../start-draft.js';
import type {ModelChoices} from '../start-models.js';
import {LimitFields, MoreOptions, type StartHost} from '../start-run-form.js';
import {domainWord, placeLabel, provisionLine} from './words.js';

/** Where Copy to starts: the folder kept for the machine, else the host's own default. */
function copyDefault(host: StartHost | undefined): string {
  if (host === undefined) return '';
  const kept = host.repositoriesDirectory ?? '';
  return kept === '' ? (host.catalogue.data?.repositoriesDirectory ?? '') : kept;
}

/** The draft as sent: a folder the person left empty is the machine's kept Repositories folder. */
export const withKeptParent = (host: StartHost | undefined, draft: ExampleDraft): ExampleDraft =>
  draft.parent === '' ? {...draft, parent: host?.repositoriesDirectory ?? ''} : draft;

/** The one-line fix for a host with no release, or an incomplete one: installed beside the binary
 * the app already pushed there, so a classified catalogue problem is actionable, not just explained. */
const INSTALL_COMMAND =
  'curl -fsSL https://raw.githubusercontent.com/uw-syfi/vibesys/main/install.sh | sh';

/**
 * What the catalogue step shows instead of the card grid when the host names a classified problem
 * (no release, or one missing files): its words, the fix as a literal command, and where to read
 * more on the host itself.
 */
function CatalogueProblemState(props: {readonly problem: CatalogueProblem}): JSX.Element {
  const words = catalogueProblemWords(props.problem);
  return (
    <Stack gap="space-3">
      <EmptyState title={words.title} body={words.detail} align="start" />
      <Text variant="code">{INSTALL_COMMAND}</Text>
      <Text tone="muted">For more, run vibesys-client host status on the host.</Text>
    </Stack>
  );
}

/** The id line of a card: the example's folder and the task in it. */
const idLine = (entry: CatalogueEntry): string =>
  `${folderName(entry)} / ${entry.card?.task ?? ''}`;

/** What a card that does not fit says in place of a place: the accelerator it needs. */
const lacking = (entry: CatalogueEntry): string => {
  const name = acceleratorName(entry.card?.hardwareName ?? '');
  return name === '' ? 'No place' : `No ${name}`;
};

/** A browse-only example's title, in words rather than its folder id. */
const browseTitle = (item: CatalogueItem): string =>
  wordsOfId(folderName({example: catalogueItemExample(item)}));

function Cards(props: {
  readonly items: readonly CatalogueItem[];
  readonly places: readonly EvalPlace[];
  readonly picked: string | null;
  readonly pick: (example: string) => void;
}): JSX.Element {
  return (
    <>
      {props.items.map(item => {
        if (item._tag === 'BrowseOnly') {
          const key = catalogueItemKey(item);
          return (
            <PickCard
              key={key}
              title={browseTitle(item)}
              id={item.example.id}
              note="Connect a machine to load its tasks and compatibility."
              tag={item.example.needsClone ? 'External source' : 'Browse offline'}
              selected={props.picked === key}
              press={() => props.pick(key)}
            />
          );
        }
        const {entry} = item;
        const variant = variantOf(entry);
        return (
          <PickCard
            key={catalogueItemKey(item)}
            title={runTitle(entry.card ?? null, folderName(entry))}
            id={idLine(entry)}
            selected={props.picked === catalogueItemKey(item)}
            press={() => props.pick(catalogueItemKey(item))}
            {...(variant === undefined
              ? catalogueItemNeedsClone(item)
                ? {tag: 'Needs network'}
                : {}
              : {tag: variant})}
            {...(fitsHere(entry, props.places) ? {} : {unavailable: lacking(entry)})}
            chips={(entry.card?.objectives ?? []).map(objective => {
              const {text, hint} = objectiveChip(objective);
              return <Chip key={objective.name} label={text} hint={hint} />;
            })}
          />
        );
      })}
    </>
  );
}

/** The Field props of the copy's status line: an error carries the host's detail on the pointer. */
function statusOf(line: ReturnType<typeof provisionLine>) {
  if (line === null) return {};
  if (!line.failed) return {help: line.text};
  return {error: line.text, ...(line.title === undefined ? {} : {errorHint: line.title})};
}

/** The picked example: where it evaluates, its limits, and Start; the repository it is copied to is under More options. */
function Selected(props: {
  readonly host: StartHost | undefined;
  readonly item: CatalogueItem | undefined;
  readonly places: readonly EvalPlace[];
  readonly example: ExampleDraft;
  readonly setExample: (draft: ExampleDraft) => void;
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly choices: ModelChoices;
  /** Why Start cannot be pressed, and whether that is said aloud, or null. */
  readonly block: {readonly reason: string; readonly quiet: boolean} | null;
  readonly start: () => void;
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
  readonly status: JSX.Element | null;
}): JSX.Element {
  const {host, item, example, places} = props;
  if (item === undefined) {
    return (
      <Stack gap="space-3">
        <Text variant="section">Selected</Text>
        <Text tone="muted">Pick a task</Text>
      </Stack>
    );
  }
  if (item._tag === 'BrowseOnly') {
    return (
      <Stack gap="space-3">
        <Stack gap="space-1">
          <Text variant="section">Example</Text>
          <Text variant="item">{browseTitle(item)}</Text>
          <Text variant="code">{item.example.id}</Text>
        </Stack>
        <Chip label={domainWord(catalogueItemDomain(item))} />
        <Text>
          This example is in the VibeSys registry. Connect a machine that provides it to see its
          tasks, hardware needs and eval compatibility.
        </Text>
        {item.example.needsClone ? (
          <Text tone="muted">
            Its source is cloned at a pinned revision when the machine provisions it, so starting it
            needs network access.
          </Text>
        ) : null}
        <Stack gap="space-1">
          <Text variant="section">Use a source checkout</Text>
          <Text tone="muted">Run this in a terminal if VibeSys is not installed locally.</Text>
          <Text variant="code">git clone https://github.com/uw-syfi/vibesys.git</Text>
        </Stack>
      </Stack>
    );
  }
  const {entry} = item;
  const set = (change: Partial<ExampleDraft>): void => props.setExample({...example, ...change});
  const fitting = placesFor(entry, places);
  const chosen = placeChoice(example.place, fitting, entry.fit);
  const line = provisionLine(host?.provision ?? {_tag: 'Idle'});
  const split = splitPath(example.parent);
  const completions =
    split === undefined
      ? []
      : completionsOf(split, host?.directories.get(listingKey(split.folder, split.hidden)));
  const defaultParent = copyDefault(host);
  return (
    <Stack gap="space-3">
      <Stack gap="space-1">
        <Text variant="section">Task</Text>
        <Text variant="item">{runTitle(entry.card ?? null, folderName(entry))}</Text>
        <Text tone="muted">{entry.card?.heading ?? ''}</Text>
      </Stack>
      <Field label="Evals on">
        <Select
          value={chosen ?? ''}
          options={fitting.map(place => ({value: place.name, label: placeLabel(place, null)}))}
          change={place => set({place})}
          mono
        />
      </Field>
      <LimitFields draft={props.draft} set={props.set} />
      <Row>
        <Button
          text="Start run"
          label="Start run"
          variant="primary"
          press={() => {
            if (props.block === null) props.start();
          }}
          {...(props.block === null
            ? {}
            : {disabledReason: props.block.reason, quietReason: props.block.quiet})}
        />
      </Row>
      {/* The copy's outcome has a line of its own outside the disclosure, kept when empty: a refusal is never hidden. */}
      <Text tone={line?.failed === true ? 'err' : 'muted'} title={line?.title ?? ''}>
        {line?.text ?? ' '}
      </Text>
      <MoreOptions
        draft={props.draft}
        set={props.set}
        choices={props.choices}
        extra={
          <>
            <Field label="Repository name">
              <TextInput
                value={nameOfExample(example, entry)}
                change={name => set({name})}
                placeholder="name"
                mono
              />
            </Field>
            <Field label="Copy to" reserveLine {...statusOf(line)}>
              <PathInput
                value={example.parent}
                change={parent => {
                  set({parent});
                  const reached = splitPath(parent);
                  if (host !== undefined && reached !== undefined)
                    props.listDirectory(host.key, reached.folder, reached.hidden, false);
                }}
                completions={completions.map(({value, detail}) => ({
                  value,
                  ...(detail === undefined ? {} : {detail}),
                }))}
                tabCompletes={split !== undefined && split.prefix !== ''}
                focus={() => {
                  const reached = splitPath(example.parent);
                  if (host !== undefined && reached !== undefined)
                    props.listDirectory(host.key, reached.folder, reached.hidden, true);
                }}
                placeholder={defaultParent === '' ? '~/' : defaultParent}
              />
            </Field>
          </>
        }
      />
      {props.status}
    </Stack>
  );
}

/**
 * The catalogue step: searchable release examples in a card grid per domain, with the picked
 * example beside them. Cards the host can provision may start; offline cards stay browseable and
 * explain what connecting or cloning enables.
 */
export function CatalogStep(props: {
  /** The machine on screen. */
  readonly host: StartHost | undefined;
  readonly stale: boolean;
  readonly places: readonly EvalPlace[];
  readonly example: ExampleDraft;
  readonly setExample: (draft: ExampleDraft) => void;
  readonly pick: (example: string) => void;
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly choices: ModelChoices;
  readonly block: {readonly reason: string; readonly quiet: boolean} | null;
  readonly start: () => void;
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
  readonly status: JSX.Element | null;
}): JSX.Element {
  const {host, example, places} = props;
  const entries = host?.catalogue.data?.entries ?? [];
  const problem = host?.catalogue.data?.problem;
  const items = catalogueItems(entries);
  const shown = visibleCatalogueItems(items, {query: example.query});
  const picked = items.find(item => catalogueItemKey(item) === example.picked);
  // An available task the machine does not fit is no pick; a browse-only example remains inspectable.
  const item = picked?._tag === 'Available' && !fitsHere(picked.entry, places) ? undefined : picked;
  const set = (change: Partial<ExampleDraft>): void => props.setExample({...example, ...change});
  const shownExamples = new Set(shown.map(catalogueItemExample)).size;
  const totalExamples = new Set(items.map(catalogueItemExample)).size;
  return (
    <Split
      asideFirst
      asideAtStart
      asideSticky
      main={
        <Stack gap="space-4">
          {problem === undefined ? null : <CatalogueProblemState problem={problem} />}
          <Row gap="space-3" wrap>
            <SearchBox
              label="Search"
              value={example.query}
              change={query => set({query})}
              placeholder="Example, model, hardware, task"
            />
            <Text tone="muted">{`${shownExamples} of ${totalExamples} examples`}</Text>
            <Text tone="muted">
              {host === undefined
                ? 'Browsing offline · connect a machine to start'
                : host.catalogue.failure === null
                  ? ''
                  : 'Machine catalogue unreadable · showing offline examples'}
            </Text>
          </Row>
          <Stale stale={props.stale}>
            <Stack gap="space-5">
              {groupCatalogueItems(shown).map(group => (
                <Stack key={group.domain} gap="space-2">
                  <Text variant="section">{`${domainWord(group.domain)} · ${group.items.length}`}</Text>
                  <PickCards label={domainWord(group.domain)} columns="fill">
                    <Cards
                      items={group.items}
                      places={places}
                      picked={item === undefined ? null : example.picked}
                      pick={props.pick}
                    />
                  </PickCards>
                </Stack>
              ))}
            </Stack>
          </Stale>
        </Stack>
      }
      aside={
        <Card>
          <Selected
            host={host}
            item={item}
            places={places}
            example={example}
            setExample={props.setExample}
            draft={props.draft}
            set={props.set}
            choices={props.choices}
            block={props.block}
            start={props.start}
            listDirectory={props.listDirectory}
            status={props.status}
          />
        </Card>
      }
    />
  );
}
