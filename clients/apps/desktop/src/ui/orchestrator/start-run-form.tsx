import type {
  ListCatalogueResponse,
  ListEvalPlacesResponse,
  ModelCatalog,
} from '@vibesys/orchestrator-proto';
import {Collapsible, Field, Grid, Select, Stack, TextInput} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {
  FolderListing,
  ProvisionProgress,
  Remote,
  RepositoryChecked,
} from '../../state/index.js';
import {
  DEFAULT_FANOUT,
  FANOUT_CHOICES,
  LEAD_CHOICES,
  LEADS,
  type LeadChoice,
  type StartDraft,
  TIME_LIMITS,
} from './start-draft.js';
import {type ModelChoices, startModel} from './start-models.js';
import {harnessWord} from './words.js';

/** A host the run can start on: its key, and what it is called on screen. */
export interface StartHost {
  readonly key: string;
  readonly label: string;
  /** What the host said it runs agents on and allows, or null before it has said. */
  readonly models: ModelCatalog | null;
  /** The host's latest answer about a repository path, with the path it was about. */
  readonly repositoryChecked: RepositoryChecked | undefined;
  /** What the host listed of each folder asked for, by `listingKey`. */
  readonly directories: ReadonlyMap<string, FolderListing>;
  /** The host's catalogue of example tasks, with its verdict on each. */
  readonly catalogue: Remote<ListCatalogueResponse>;
  /** The host's eval places. */
  readonly evalPlaces: Remote<ListEvalPlacesResponse>;
  /** The Repositories folder kept for this machine, empty when none: where Copy to starts. */
  readonly repositoriesDirectory?: string;
  /** Where the repository being made from an example stands. */
  readonly provision: ProvisionProgress;
}

const timeWord = (minutes: number): string =>
  minutes < 60 ? `${minutes} min` : `${minutes / 60} h`;

/** The model control's options: the tool's default first where the host leaves it free, then the models. */
const modelOptions = (choices: ModelChoices) => [
  ...(choices.pinned ? [] : [{value: '', label: 'Default'}]),
  ...choices.models.map(id => ({value: id, label: id})),
];

/** The run's bounds: how much it may cost and how long it may take, each none unless set. */
export function LimitFields(props: {
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
}): JSX.Element {
  const {draft, set} = props;
  return (
    <Grid min="tile" gap="space-3">
      <Field label="Cost limit ($)">
        <TextInput
          value={draft.costLimit}
          change={costLimit => set({costLimit})}
          placeholder="None"
          mono
        />
      </Field>
      <Field label="Time limit">
        <Select
          value={String(draft.timeLimit)}
          options={TIME_LIMITS.map(minutes => ({
            value: String(minutes),
            label: minutes === null ? 'None' : timeWord(minutes),
          }))}
          change={chosen =>
            set({timeLimit: TIME_LIMITS.find(minutes => String(minutes) === chosen) ?? null})
          }
        />
      </Field>
    </Grid>
  );
}

/**
 * The lead's tool and model, how many agents at most, and steering, under one disclosure that is
 * the last thing of its column, so opening it moves nothing. `extra` is what else the screen keeps
 * there.
 */
export function MoreOptions(props: {
  readonly draft: StartDraft;
  readonly set: (change: Partial<StartDraft>) => void;
  readonly choices: ModelChoices;
  readonly extra?: JSX.Element;
}): JSX.Element {
  const {draft, set, choices} = props;
  const [open, setOpen] = useState(false);
  const model = startModel(draft.model, choices);
  return (
    <Collapsible label="More options" open={open} toggle={setOpen}>
      <Stack gap="space-3">
        <Grid min="tile" gap="space-3">
          <Field label="Lead tool">
            <Select
              value={draft.lead}
              options={LEAD_CHOICES.map(choice => ({
                value: choice,
                label: harnessWord(LEADS[choice]),
              }))}
              change={(choice: LeadChoice) => set({lead: choice, model: ''})}
            />
          </Field>
          <Field label="Model">
            <Select
              value={model}
              options={modelOptions(choices)}
              change={chosen => set({model: chosen})}
              mono
            />
          </Field>
        </Grid>
        <Field label="Agents at once">
          <Select
            value={String(draft.fanout)}
            options={FANOUT_CHOICES.map(count => ({
              value: String(count),
              label: count === DEFAULT_FANOUT ? `${count} (default)` : String(count),
            }))}
            change={value => set({fanout: Number(value)})}
          />
        </Field>
        <Field label="Steering">
          <TextInput
            value={draft.steering}
            change={steering => set({steering})}
            placeholder="Optional"
            rows={2}
          />
        </Field>
        {props.extra}
      </Stack>
    </Collapsible>
  );
}
