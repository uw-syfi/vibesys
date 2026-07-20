import {writeFile} from 'node:fs/promises';
import {
  BoxRenderable,
  CliRenderEvents,
  type CliRenderer,
  createCliRenderer,
  InputRenderable,
  TextRenderable,
} from '@opentui/core';
import type {SetupDefaults, SetupSelection} from './setup-model.js';
import {validateSetupSelection} from './setup-model.js';

const rawDefaults = process.env['VIBESYS_SETUP_DEFAULTS'];
const resultPath = process.env['VIBESYS_SETUP_RESULT'];
if (!rawDefaults || !resultPath) {
  throw new Error('VIBESYS_SETUP_DEFAULTS and VIBESYS_SETUP_RESULT are required');
}

const defaults = JSON.parse(rawDefaults) as SetupDefaults;
const renderer = await createCliRenderer({exitOnCtrlC: false});
const form = createSetupForm(renderer, defaults);
renderer.start();

await new Promise<void>(resolve => renderer.once(CliRenderEvents.DESTROY, resolve));
if (form.selection !== undefined) {
  await writeFile(resultPath, JSON.stringify(form.selection), 'utf8');
}
form.destroy();

interface SetupForm {
  readonly selection: SetupSelection | undefined;
  destroy(): void;
}

function createSetupForm(renderer: CliRenderer, defaults: SetupDefaults): SetupForm {
  const root = new BoxRenderable(renderer, {
    id: 'setup',
    width: '100%',
    height: '100%',
    flexDirection: 'column',
    paddingLeft: 2,
    paddingRight: 2,
    paddingTop: 1,
  });
  const title = new TextRenderable(renderer, {
    id: 'setup-title',
    height: 2,
    fg: '#22d3ee',
    content: 'VibeSys · New experiment',
  });
  const instructions = new TextRenderable(renderer, {
    id: 'setup-instructions',
    height: 2,
    fg: '#94a3b8',
    content: 'Tab / Shift-Tab: move · Enter: launch · Esc: cancel · Clear owner for local-only',
  });
  const error = new TextRenderable(renderer, {
    id: 'setup-error',
    height: 2,
    fg: '#f87171',
    content: '',
  });

  const entries = [
    createField(renderer, 'Input bundle', defaults.input_path, 'examples/<input>'),
    createField(renderer, 'Experiment name', defaults.experiment_name, 'experiment name'),
    createField(
      renderer,
      'Repository owner',
      defaults.repository_owner ?? '',
      'local-only if empty',
    ),
    createField(renderer, 'Repository name', defaults.repository_name, 'repository name'),
    createField(renderer, 'Visibility', defaults.visibility, 'private | public | internal'),
  ];
  root.add(title);
  root.add(instructions);
  for (const entry of entries) root.add(entry.box);
  root.add(error);
  renderer.root.add(root);

  let selected: SetupSelection | undefined;
  let focused = 0;
  entries[focused]?.input.focus();

  const readSelection = (): SetupSelection => ({
    inputPath: entries[0]?.input.value ?? '',
    experimentName: entries[1]?.input.value ?? '',
    repositoryOwner: entries[2]?.input.value ?? '',
    repositoryName: entries[3]?.input.value ?? '',
    visibility: entries[4]?.input.value ?? '',
  });
  const moveFocus = (offset: number): void => {
    entries[focused]?.input.blur();
    focused = (focused + offset + entries.length) % entries.length;
    entries[focused]?.input.focus();
  };
  const onKey = (key: {name: string; shift: boolean; ctrl: boolean; preventDefault(): void}) => {
    if (key.name === 'tab') {
      moveFocus(key.shift ? -1 : 1);
      key.preventDefault();
      return;
    }
    if (key.name === 'escape' || (key.ctrl && key.name === 'c')) {
      key.preventDefault();
      renderer.destroy();
      return;
    }
    if (key.name !== 'return' && key.name !== 'enter') return;
    key.preventDefault();
    const candidate = readSelection();
    const validationError = validateSetupSelection(candidate);
    if (validationError !== undefined) {
      error.content = validationError;
      return;
    }
    selected = candidate;
    renderer.destroy();
  };
  renderer.keyInput.on('keypress', onKey);

  return {
    get selection(): SetupSelection | undefined {
      return selected;
    },
    destroy(): void {
      renderer.keyInput.off('keypress', onKey);
      root.destroyRecursively();
    },
  };
}

function createField(
  renderer: CliRenderer,
  label: string,
  value: string,
  placeholder: string,
): {box: BoxRenderable; input: InputRenderable} {
  const box = new BoxRenderable(renderer, {
    height: 3,
    width: '100%',
    border: true,
    borderStyle: 'rounded',
    borderColor: '#475569',
    title: ` ${label} `,
    paddingLeft: 1,
    paddingRight: 1,
  });
  const input = new InputRenderable(renderer, {
    width: '100%',
    value,
    placeholder,
    textColor: '#f8fafc',
    focusedTextColor: '#f8fafc',
  });
  box.add(input);
  return {box, input};
}
