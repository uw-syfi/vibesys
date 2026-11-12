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
 * The edit of one folder. A generation belongs to both the text and the save started from it, so an
 * older completion cannot clear a replacement edit or attach its refusal to that replacement.
 */
type FolderEdit =
  | {readonly kind: 'clean'; readonly generation: number}
  | {
      readonly kind: 'draft';
      readonly generation: number;
      readonly value: string;
      readonly refusal: string | null;
    }
  | {readonly kind: 'saving'; readonly generation: number; readonly value: string};

const SAVE_TARGET_CHARACTERS = 12;

/**
 * A Save names both the setting and its machine. A person-chosen long alias is shortened only in
 * the face; its accessible name and hover text keep the whole alias.
 */
function saveWords(row: FolderRow): {readonly text: string; readonly whole: string} {
  const target = row.key === 'local' ? 'local' : row.label;
  const points = [...target];
  const face =
    points.length <= SAVE_TARGET_CHARACTERS
      ? target
      : `${points.slice(0, SAVE_TARGET_CHARACTERS - 1).join('')}…`;
  return {text: `Save ${face} folder`, whole: `Save ${target} folder`};
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
  const [edit, setEdit] = useState<FolderEdit>({kind: 'clean', generation: 0});
  const value = edit.kind === 'clean' ? row.folder : edit.value;
  const check = checkRepositoriesFolder(value);
  const changed = check.ok ? check.directory !== row.folder : true;
  const error = !check.ok ? check.message : edit.kind === 'draft' ? edit.refusal : null;
  const save = saveWords(row);
  const commit = (): void => {
    if (!check.ok || !changed || edit.kind === 'saving') return;
    const generation = edit.generation + 1;
    const submitted = check.directory;
    setEdit({kind: 'saving', generation, value: submitted});
    props.save(row.key, submitted, refusal => {
      setEdit(current => {
        if (current.kind !== 'saving' || current.generation !== generation) return current;
        return refusal === null
          ? {kind: 'clean', generation}
          : {kind: 'draft', generation, value: submitted, refusal};
      });
    });
  };
  return (
    <Field label={row.label} reserveLine {...(error === null ? {} : {error})}>
      <Row gap="space-3">
        <TextInput
          mono
          value={value}
          change={next => {
            setEdit(current => ({
              kind: 'draft',
              generation: current.generation + 1,
              value: next,
              refusal: null,
            }));
          }}
          submit={commit}
          placeholder="Host default"
        />
        <Button
          variant="secondary"
          text={save.text}
          label={save.whole}
          hint={save.whole}
          press={commit}
          {...(!check.ok || !changed || edit.kind === 'saving'
            ? {
                disabledReason:
                  edit.kind === 'saving' ? 'Saving' : !check.ok ? check.message : 'No change',
                quietReason: true,
              }
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
