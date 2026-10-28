/**
 * The files an attempt changed, each opening its part of the attempt's diff with the lines it
 * adds and removes. The list is the run's whole list; the diff arrives once the page has read it.
 */
import type {CandidateAttempt} from '@vibesys/run-state/candidates';
import {Collapsible, Markdown, Stack, Text} from '@vibesys/ui';
import {type JSX, useMemo, useState} from 'react';
import {
  attemptFiles,
  CHANGED_FILES,
  type ChangedFile,
  changeCounts,
  diffBlock,
  filePatches,
  type RunModel,
} from '../../../../state/index.js';

function FilePatch(props: {readonly file: ChangedFile; readonly patch: string | null}) {
  if (props.file.length === 0)
    return (
      <Text variant="small" tone="muted">
        {CHANGED_FILES.cut}
      </Text>
    );
  if (props.patch === null)
    return (
      <Text variant="small" tone="muted">
        {CHANGED_FILES.reading}
      </Text>
    );
  return <Markdown text={diffBlock(props.patch)} />;
}

export function ChangedFiles(props: {
  readonly model: RunModel;
  readonly attempt: CandidateAttempt;
}): JSX.Element | null {
  const [open, setOpen] = useState<ReadonlySet<string>>(new Set());
  const files = useMemo(
    () => attemptFiles(props.model, props.attempt),
    [props.model, props.attempt],
  );
  const diff = props.model.diffs?.get(props.attempt.span)?.text ?? null;
  const patches = useMemo(() => (diff === null ? [] : filePatches(diff, files)), [diff, files]);
  if (files.length === 0) return null;
  const toggle = (path: string, opened: boolean) =>
    setOpen(previous => {
      const next = new Set(previous);
      if (opened) next.add(path);
      else next.delete(path);
      return next;
    });
  return (
    <Stack gap="space-1">
      <Text variant="small" tone="muted">
        {`${files.length} ${CHANGED_FILES.title}`}
      </Text>
      {files.map((file, index) => {
        const patch = patches[index] ?? null;
        return (
          <Collapsible
            key={file.path}
            label={file.path}
            mono
            {...(patch === null ? {} : {meta: changeCounts(patch)})}
            open={open.has(file.path)}
            toggle={opened => toggle(file.path, opened)}
          >
            <FilePatch file={file} patch={patch} />
          </Collapsible>
        );
      })}
    </Stack>
  );
}
