import {Button, Card, Field, Row, Stack, TextInput} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import {checkRepositoriesFolder, REPOSITORIES_FIELD} from '../../state/index.js';

/** One machine and the Repositories folder kept for it; empty when none is kept. */
interface FolderRow {
  readonly key: string;
  readonly label: string;
  readonly folder: string;
}

/** What the Repositories card does: keep a folder for a machine, and say why when it is refused. */
export interface RepositoriesControls {
  readonly rows: readonly FolderRow[];
  /** Keeps `folder` (empty clears it) for the machine; `done` gets the refusal, or null when kept. */
  readonly save: (key: string, folder: string, done: (refusal: string | null) => void) => void;
}

/**
 * One machine's folder: the input, a Save beside it, and the line under it that says why a folder is
 * refused (this app's check first, then the main process's). The line keeps its room when empty, so
 * nothing below it moves.
 */
function FolderField(props: {
  readonly row: FolderRow;
  readonly save: RepositoriesControls['save'];
}): JSX.Element {
  const {row} = props;
  const [typed, setTyped] = useState<string | null>(null);
  const [refused, setRefused] = useState<string | null>(null);
  const value = typed ?? row.folder;
  const check = checkRepositoriesFolder(value);
  const changed = check.ok ? check.directory !== row.folder : true;
  const error = !check.ok ? check.message : refused;
  const commit = (): void => {
    if (!check.ok || !changed) return;
    props.save(row.key, check.directory, refusal => {
      setRefused(refusal);
      if (refusal === null) setTyped(null);
    });
  };
  return (
    <Field label={row.label} reserveLine {...(error === null ? {} : {error})}>
      <Row gap="space-3">
        <TextInput
          mono
          value={value}
          change={next => {
            setTyped(next);
            setRefused(null);
          }}
          submit={commit}
          placeholder="Host default"
        />
        <Button
          variant="secondary"
          text="Save"
          press={commit}
          {...(!check.ok || !changed
            ? {disabledReason: !check.ok ? check.message : 'No change', quietReason: true}
            : {})}
        />
      </Row>
    </Field>
  );
}

/** Settings of the machines: where each one's new repositories are made. */
export function RepositoriesCard(props: {readonly controls: RepositoriesControls}): JSX.Element {
  return (
    <Card section={REPOSITORIES_FIELD}>
      <Stack gap="space-4">
        {props.controls.rows.map(row => (
          <FolderField key={row.key} row={row} save={props.controls.save} />
        ))}
      </Stack>
    </Card>
  );
}
