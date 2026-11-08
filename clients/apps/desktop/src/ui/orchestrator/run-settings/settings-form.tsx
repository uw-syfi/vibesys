import type {ModelCatalog} from '@vibesys/orchestrator-proto';
import {Field, Grid, Select, type SelectOption, TextInput} from '@vibesys/ui';
import type {JSX} from 'react';
import type {RunSettings} from '../../../state/index.js';
import {FANOUT_CHOICES, LEAD_CHOICES, LEADS, type LeadChoice, TIME_LIMITS} from '../start-draft.js';
import {modelChoices} from '../start-models.js';
import {harnessWord} from '../words.js';
import {
  costLine,
  fanoutLine,
  leadLine,
  NO_LIMIT_WORD,
  timeLine,
  timeWord,
} from './settings-words.js';
import {NO_LIMIT, type SettingsDraft} from './update-draft.js';

/** The time limits offered, in seconds, with the run's own when it is not one of them. */
function timeOptions(settings: RunSettings): SelectOption<string>[] {
  const offered = TIME_LIMITS.flatMap(minutes => (minutes === null ? [] : [minutes * 60]));
  const own = settings.time.limitSeconds;
  const seconds =
    own === null || offered.includes(own) ? offered : [...offered, own].sort((a, b) => a - b);
  return [
    {value: NO_LIMIT, label: NO_LIMIT_WORD},
    ...seconds.map(value => ({value: String(value), label: timeWord(value)})),
  ];
}

/** The fanouts offered, with the run's own when it is not one of them. */
function fanoutOptions(settings: RunSettings): SelectOption<string>[] {
  const counts: number[] = [...FANOUT_CHOICES];
  if (!counts.includes(settings.fanout.cap)) counts.push(settings.fanout.cap);
  return counts.sort((a, b) => a - b).map(count => ({value: String(count), label: String(count)}));
}

/** The models offered for the lead's tool: the host's choices, the tool's default where it is free, and the lead's own. */
function modelOptions(catalog: ModelCatalog | null, draft: SettingsDraft): SelectOption<string>[] {
  const choices = modelChoices(catalog, LEADS[draft.lead]);
  const models =
    choices.models.includes(draft.model) || draft.model === ''
      ? choices.models
      : [...choices.models, draft.model];
  return [
    ...(choices.pinned ? [] : [{value: '', label: 'Default'}]),
    ...models.map(id => ({value: id, label: id})),
  ];
}

/** The four settings as fields, each with what the run has used of it on the line below. */
export function SettingsFields(props: {
  readonly settings: RunSettings;
  readonly models: ModelCatalog | null;
  readonly draft: SettingsDraft;
  readonly set: (change: Partial<SettingsDraft>) => void;
  readonly costError: string | undefined;
}): JSX.Element {
  const {settings, models, draft, set} = props;
  const pending = leadLine(settings);
  return (
    <Grid min="field" gap="space-3">
      <Field
        label="Cost limit ($)"
        help={costLine(settings)}
        {...(props.costError === undefined ? {} : {error: props.costError})}
      >
        <TextInput
          value={draft.cost}
          change={cost => set({cost})}
          placeholder={NO_LIMIT_WORD}
          mono
        />
      </Field>
      <Field label="Time limit" help={timeLine(settings)}>
        <Select value={draft.time} options={timeOptions(settings)} change={time => set({time})} />
      </Field>
      <Field label="Agents at once" help={fanoutLine(settings)}>
        <Select
          value={draft.fanout}
          options={fanoutOptions(settings)}
          change={fanout => set({fanout})}
        />
      </Field>
      <Field label="Lead" reserveLine {...(pending === undefined ? {} : {help: pending})}>
        <Select
          value={draft.lead}
          options={LEAD_CHOICES.map(choice => ({
            value: choice,
            label: harnessWord(LEADS[choice]),
          }))}
          change={(lead: LeadChoice) => set({lead, model: ''})}
        />
      </Field>
      <Field label="Model" reserveLine>
        <Select
          value={draft.model}
          options={modelOptions(models, draft)}
          change={model => set({model})}
          mono
        />
      </Field>
    </Grid>
  );
}
