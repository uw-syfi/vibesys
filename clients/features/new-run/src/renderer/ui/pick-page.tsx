/**
 * Step 1, "What do you want to optimize?": an example's task from the machine's catalogue, or a
 * task of the user's own project (a recent one, or a folder opened by path). Before the user has
 * chosen a side it opens on their projects when they have any, else on the examples.
 */
import {Button, Row, SearchBox, SegmentedControl, Stack} from '@vibesys/ui';
import {type JSX, useState} from 'react';
import type {Domain, NewRunContext} from '../../state/index.js';
import type {CardProps} from './card-props.js';
import {ExamplesPane} from './example-pages.js';
import {type FolderCompletion, ProjectsPane} from './project-pages.js';
import {type MachineOption, recentChoices} from './view.js';

type From = 'example' | 'project';

/** The side shown: the one chosen, else the user's projects when they have any. */
function shownSide(context: NewRunContext): From {
  return context.answers.from ?? (recentChoices(context.recent).length > 0 ? 'project' : 'example');
}

export function PickPage(
  props: CardProps & {
    readonly machines: readonly MachineOption[];
    readonly completion: FolderCompletion;
  },
): JSX.Element {
  const {context, dispatch} = props;
  const [folder, setFolder] = useState(false);
  const [domain, setDomain] = useState<Domain | null>(null);
  const [search, setSearch] = useState('');
  const side = shownSide(context);
  const recents = recentChoices(context.recent).length;
  const choose = (from: From): void => dispatch({type: 'answer', change: {kind: 'from', from}});
  return (
    <Stack gap="space-4">
      <Row justify="between" wrap gap="space-3">
        <Row gap="space-3" wrap>
          <SegmentedControl
            label="Start from"
            size="sm"
            options={[
              {value: 'example', label: 'Examples'},
              {
                value: 'project',
                label: recents === 0 ? 'Your projects' : `Your projects ${recents}`,
              },
            ]}
            value={side}
            change={choose}
          />
          <Button
            size="sm"
            text="Open a folder…"
            press={() => {
              setFolder(true);
              choose('project');
            }}
          />
        </Row>
        {side === 'example' ? (
          <SearchBox
            label="Search examples"
            value={search}
            change={setSearch}
            placeholder="Model, GPU, structure…"
          />
        ) : null}
      </Row>
      {side === 'example' ? (
        <ExamplesPane {...props} domain={domain} changeDomain={setDomain} search={search} />
      ) : (
        <ProjectsPane {...props} folder={folder} />
      )}
    </Stack>
  );
}
