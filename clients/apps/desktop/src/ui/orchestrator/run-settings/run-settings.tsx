import type {ModelCatalog} from '@vibesys/orchestrator-proto';
import {Button, Chip, Dialog, Stack} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {CommandProgress, RunSettings, TimelineRow} from '../../../state/index.js';
import {CommandLine} from '../command-line.js';
import {SettingsFields} from './settings-form.js';
import {SettingsTimeline} from './settings-timeline.js';
import {leadWord} from './settings-words.js';
import {asked, draftOf, type RunUpdateRequest, type SettingsDraft} from './update-draft.js';

const COST_ERROR = 'A positive number of dollars';

/** Why Apply cannot be pressed, or undefined when it can: a word, shown as the button's reason. */
function applyBlock(
  outcome: ReturnType<typeof asked>,
  progress: CommandProgress | undefined,
): string | undefined {
  if (progress?._tag === 'Sending') return 'Sending.';
  if (outcome.kind === 'invalid') return COST_ERROR;
  return outcome.kind === 'nothing' ? 'No change.' : undefined;
}

/**
 * A run's live settings, from the page's header: one Settings button that opens a sheet over the page
 * with the four settings (cost limit, time limit, agents at once, lead) as fields, what the run has
 * used of each, and the timeline of the changes made. The sheet is an overlay, so nothing of the page
 * moves when it opens, and Apply asks the host for the whole change at once: the sheet stays open and
 * says in one line how the host answered, in its words when it refused.
 */
export function RunSettingsControl(props: {
  readonly settings: RunSettings;
  readonly timeline: readonly TimelineRow[];
  readonly models: ModelCatalog | null;
  /** The run can still be changed; a run that is over shows only what was changed. */
  readonly live: boolean;
  /** The last update of this run. */
  readonly progress: CommandProgress | undefined;
  readonly update: (request: RunUpdateRequest) => void;
  readonly retry: () => void;
}): JSX.Element {
  const {settings, progress} = props;
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState<SettingsDraft>(() => draftOf(settings));
  // The outcome of an update made before the sheet opened is not this sheet's to show.
  const [before, setBefore] = useState<CommandProgress | undefined>(undefined);
  const outcome = asked(draft, settings);
  const block = applyBlock(outcome, progress);
  const shown = progress === before ? undefined : progress;
  const show = (): void => {
    setDraft(draftOf(settings));
    setBefore(progress);
    setOpen(true);
  };
  return (
    <>
      <Button text="Adjust" variant="secondary" press={show} />
      <Dialog
        open={open}
        title="Adjust run"
        dismiss={() => setOpen(false)}
        actions={
          <>
            <CommandLine progress={shown} done="Update" retry={props.retry} />
            <Button text={props.live ? 'Cancel' : 'Close'} press={() => setOpen(false)} />
            {props.live ? (
              <Button
                text="Apply"
                variant="primary"
                press={() => outcome.kind === 'update' && props.update(outcome.request)}
                {...(block === undefined ? {} : {disabledReason: block, quietReason: true})}
              />
            ) : null}
          </>
        }
      >
        <Stack gap="space-4">
          {props.live ? (
            <SettingsFields
              settings={settings}
              models={props.models}
              draft={draft}
              set={change => setDraft({...draft, ...change})}
              costError={outcome.kind === 'invalid' ? COST_ERROR : undefined}
            />
          ) : null}
          <SettingsTimeline rows={props.timeline} />
        </Stack>
      </Dialog>
    </>
  );
}

/** The lead change waiting for the lead's turn to end, as run state beside the title; none when none waits. */
export function PendingLead(props: {readonly settings: RunSettings}): JSX.Element | null {
  const {pending} = props.settings.lead;
  return pending === null ? null : (
    <Chip label="Lead pending" tone="live" dot hint={leadWord(pending)} />
  );
}
