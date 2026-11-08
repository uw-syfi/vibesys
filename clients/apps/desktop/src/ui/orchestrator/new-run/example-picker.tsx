import type {CatalogueEntry, EvalPlace} from '@vibesys/orchestrator-proto';
import {
  Field,
  PathInput,
  PickList,
  PickRow,
  Row,
  SearchBox,
  SegmentedControl,
  Select,
  type SelectOption,
  Stack,
  Text,
  TextInput,
} from '@vibesys/ui';
import type {JSX} from 'react';
import {
  completionsOf,
  type ExampleDraft,
  fits,
  fitWord,
  folderName,
  groupByDomain,
  listingKey,
  nameOfExample,
  type PlaceWord,
  placeChoice,
  placeWord,
  splitPath,
  visibleEntries,
} from '../../../state/index.js';
import {runTitle} from '../fleet-words.js';
import type {StartHost} from '../start-run-form.js';
import {domainWord, fitTone, placeLabel, provisionLine} from './words.js';

/** The select's option for no place yet: it keeps the control's height before an example is picked. */
const NO_PLACE: SelectOption<string> = {value: '', label: '—'};

/** The places of the select, each with the host's word for it on the picked entry. */
function placeOptions(
  places: readonly EvalPlace[],
  entry: CatalogueEntry | undefined,
): readonly SelectOption<string>[] {
  if (entry === undefined) return [NO_PLACE];
  return places.map((place): SelectOption<string> => {
    const word: PlaceWord = placeWord(place, entry.fit);
    return word === 'fits'
      ? {value: place.name, label: placeLabel(place, word)}
      : {value: place.name, label: placeLabel(place, null), disabledReason: word};
  });
}

function EntryRows(props: {
  readonly entries: readonly CatalogueEntry[];
  readonly picked: string | null;
  readonly pick: (example: string) => void;
}): JSX.Element {
  return (
    <>
      {props.entries.map(entry => {
        const word = fitWord(entry.fit);
        const folder = folderName(entry);
        return (
          <PickRow
            key={entry.example}
            id={entry.example}
            title={runTitle(entry.card ?? null, folder)}
            summary={folder}
            badges={[{label: word, tone: fitTone(word)}]}
            select={() => props.pick(entry.example)}
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

/** The one line that tells how the read of the catalogue stands; it keeps its place whatever it says. */
function catalogueStatus(host: StartHost | undefined, shown: number): string {
  if (host === undefined) return '';
  const {catalogue} = host;
  if (catalogue.failure !== null) return 'Catalogue unreadable';
  if (catalogue.reading) return 'Reading';
  return catalogue.data === null ? '' : `${shown}`;
}

/**
 * The catalogue half of New run: a search and the host's fit filter, the entries grouped by domain
 * with the host's verdict on each, and under them the picked example's eval place, the name of the
 * repository made from it and the folder it goes in. The status of the copy has a line of its own
 * under the folder, which keeps its place whatever it says.
 */
export function ExamplePicker(props: {
  readonly host: StartHost | undefined;
  readonly draft: ExampleDraft;
  readonly setDraft: (draft: ExampleDraft) => void;
  readonly pick: (example: string) => void;
  readonly listDirectory: (host: string, folder: string, hidden: boolean, fresh: boolean) => void;
}): JSX.Element {
  const {host, draft} = props;
  const set = (change: Partial<ExampleDraft>): void => props.setDraft({...draft, ...change});
  const entries = host?.catalogue.data?.entries ?? [];
  const places = host?.evalPlaces.data?.places ?? [];
  const fitting = entries.filter(fits).length;
  const shown = visibleEntries(entries, {query: draft.query, fitsOnly: draft.fitsOnly});
  const picked = entries.find(entry => entry.example === draft.picked);
  const chosen = picked === undefined ? undefined : placeChoice(draft.place, places, picked.fit);
  const line = provisionLine(host?.provision ?? {_tag: 'Idle'});
  const split = splitPath(draft.parent);
  const parentCompletions =
    split === undefined
      ? []
      : completionsOf(split, host?.directories.get(listingKey(split.folder, split.hidden)));
  const defaultParent = host?.catalogue.data?.repositoriesDirectory ?? '';
  return (
    <Stack gap="space-3">
      <Row gap="space-2" wrap>
        <SearchBox
          label="Search"
          value={draft.query}
          change={query => set({query})}
          placeholder="Model, hardware, task"
        />
        <SegmentedControl
          label="Show"
          size="sm"
          value={draft.fitsOnly ? 'fits' : 'all'}
          options={[
            {value: 'fits', label: `Fits · ${fitting}`},
            {value: 'all', label: `All · ${entries.length}`},
          ]}
          change={value => set({fitsOnly: value === 'fits'})}
        />
        <Text tone="muted">{catalogueStatus(host, shown.length)}</Text>
      </Row>
      <Stack gap="space-3">
        {groupByDomain(shown).map(group => (
          <Stack key={group.domain} gap="space-1">
            <Text variant="section">{`${domainWord(group.domain)} · ${group.entries.length}`}</Text>
            <PickList label={domainWord(group.domain)} selected={draft.picked ?? ''}>
              <EntryRows entries={group.entries} picked={draft.picked} pick={props.pick} />
            </PickList>
          </Stack>
        ))}
      </Stack>
      <Field label="Evals on">
        <Select
          value={chosen ?? ''}
          options={placeOptions(places, picked)}
          change={place => set({place})}
          mono
        />
      </Field>
      <Field label="Repository name">
        <TextInput
          value={picked === undefined ? '' : nameOfExample(draft, picked)}
          change={name => set({name})}
          placeholder="name"
          mono
        />
      </Field>
      <Field label="Copy to" reserveLine {...statusOf(line)}>
        <PathInput
          value={draft.parent}
          change={parent => {
            set({parent});
            const reached = splitPath(parent);
            if (host !== undefined && reached !== undefined)
              props.listDirectory(host.key, reached.folder, reached.hidden, false);
          }}
          completions={parentCompletions.map(({value, detail}) => ({
            value,
            ...(detail === undefined ? {} : {detail}),
          }))}
          tabCompletes={split !== undefined && split.prefix !== ''}
          focus={() => {
            const reached = splitPath(draft.parent);
            if (host !== undefined && reached !== undefined)
              props.listDirectory(host.key, reached.folder, reached.hidden, true);
          }}
          placeholder={defaultParent === '' ? '~/' : defaultParent}
        />
      </Field>
    </Stack>
  );
}
