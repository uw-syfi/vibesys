import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
import {act} from 'react';
import {checkRepositoriesFolder, initialAppearance} from '../../state/index.js';
import {fieldOf, typeInto} from '../../testing/dom/fields.js';
import {installDom, mount, type Screen} from '../../testing/dom/harness.js';
import type {RepositoriesControls} from './repositories-card.js';
import {SettingsScreen} from './settings-screen.js';

installDom();

interface Saved {
  readonly key: string;
  readonly folder: string;
}

function open(
  options: {
    readonly refuse?: string;
    readonly defer?: boolean;
    readonly rows?: readonly {
      readonly key: string;
      readonly label: string;
      readonly folder: string;
    }[];
  } = {},
) {
  const saved: Saved[] = [];
  const completions: ((refusal: string | null) => void)[] = [];
  const repositories: RepositoriesControls = {
    rows: options.rows ?? [
      {key: 'local', label: 'This machine', folder: ''},
      {key: 'ssh:host-a', label: 'host-a', folder: '~/work'},
    ],
    save: (key, folder, done) => {
      saved.push({key, folder});
      if (options.defer === true) completions.push(done);
      else done(options.refuse ?? null);
    },
  };
  const screen = mount(
    <SettingsScreen
      appearance={initialAppearance('light')}
      choose={() => undefined}
      repositories={repositories}
    />,
  );
  return {screen, saved, completions};
}

const input = (screen: Screen, label: string) => fieldOf<HTMLInputElement>(screen, label);
const save = (screen: Screen, label: string): HTMLElement => {
  const field = input(screen, label).closest('.vs-field');
  const button = field?.querySelector<HTMLElement>('button');
  if (button === undefined) throw new Error(`no Save beside ${label}`);
  if (button === null) throw new Error(`no Save beside ${label}`);
  return button;
};
const disabled = (button: HTMLElement): boolean => button.getAttribute('aria-disabled') === 'true';

describe('the Repositories folder of each machine', () => {
  it('shows the folder kept for each machine, and none as empty', () => {
    const {screen} = open();
    expect(input(screen, 'This machine').value).toBe('');
    expect(input(screen, 'host-a').value).toBe('~/work');
    screen.unmount();
  });

  it('names the folder and machine on every Save, clipping only a long visible alias', () => {
    const long = 'gpu-training-cluster-login-node-01.region-east.research.example.internal';
    const {screen} = open({
      rows: [
        {key: 'local', label: 'This machine', folder: ''},
        {key: 'ssh:host-a', label: 'host-a', folder: ''},
        {key: `ssh:${long}`, label: long, folder: ''},
      ],
    });
    expect(save(screen, 'This machine').textContent).toBe('Save local folder');
    expect(save(screen, 'host-a').textContent).toBe('Save host-a folder');
    const clipped = save(screen, long);
    expect(clipped.textContent).toBe('Save gpu-trainin… folder');
    expect(clipped.getAttribute('aria-label')).toBe(`Save ${long} folder`);
    expect(clipped.getAttribute('title')).toBe(`Save ${long} folder`);
    screen.unmount();
  });

  it('refuses a relative folder inline, naming the field, and sends nothing', () => {
    const {screen, saved} = open();
    typeInto(input(screen, 'This machine'), 'repos');
    expect(screen.text()).toContain('Repositories: absolute path or ~');
    expect(disabled(save(screen, 'This machine'))).toBe(true);
    screen.press(save(screen, 'This machine'));
    expect(saved).toEqual([]);
    screen.unmount();
  });

  it('keeps a valid folder for that machine and clears one on empty', () => {
    const {screen, saved} = open();
    typeInto(input(screen, 'This machine'), ' ~/runs ');
    screen.press(save(screen, 'This machine'));
    typeInto(input(screen, 'host-a'), '');
    screen.press(save(screen, 'host-a'));
    expect(saved).toEqual([
      {key: 'local', folder: '~/runs'},
      {key: 'ssh:host-a', folder: ''},
    ]);
    screen.unmount();
  });

  it('shows the main process’s refusal beside the field and keeps what was typed', () => {
    const {screen} = open({refuse: 'Repositories: host not saved'});
    typeInto(input(screen, 'This machine'), '/data');
    screen.press(save(screen, 'This machine'));
    expect(screen.text()).toContain('Repositories: host not saved');
    expect(input(screen, 'This machine').value).toBe('/data');
    screen.unmount();
  });

  it('keeps a newer edit when an older save succeeds or is refused', () => {
    for (const refusal of [null, 'Repositories: host not saved']) {
      const {screen, completions} = open({defer: true});
      typeInto(input(screen, 'This machine'), '/first');
      screen.press(save(screen, 'This machine'));

      typeInto(input(screen, 'This machine'), '/second');
      act(() => completions[0]?.(refusal));

      expect(input(screen, 'This machine').value).toBe('/second');
      expect(screen.text()).not.toContain('Repositories: host not saved');
      expect(disabled(save(screen, 'This machine'))).toBe(false);
      screen.unmount();
    }
  });

  it('only applies the newest save completion across later edits and saves', () => {
    fc.assert(
      fc.property(fc.nat(), fc.boolean(), (suffix, olderRefused) => {
        const replacement = `/second-${suffix}`;
        const {screen, completions} = open({defer: true});
        typeInto(input(screen, 'This machine'), '/first');
        screen.press(save(screen, 'This machine'));
        typeInto(input(screen, 'This machine'), replacement);
        screen.press(save(screen, 'This machine'));

        act(() => completions[0]?.(olderRefused ? 'Repositories: old refusal' : null));
        expect(input(screen, 'This machine').value).toBe(replacement);
        expect(screen.text()).not.toContain('Repositories: old refusal');

        act(() => completions[1]?.('Repositories: newest refusal'));
        expect(input(screen, 'This machine').value).toBe(replacement);
        expect(screen.text()).toContain('Repositories: newest refusal');
        screen.unmount();
      }),
      {numRuns: 20},
    );
  });

  it('sends only folders the check accepts, whatever is typed', () => {
    fc.assert(
      fc.property(fc.string({maxLength: 20}), typed => {
        const {screen, saved} = open();
        typeInto(input(screen, 'This machine'), typed);
        screen.press(save(screen, 'This machine'));
        for (const sent of saved) {
          const check = checkRepositoriesFolder(sent.folder);
          expect(check.ok && check.directory === sent.folder).toBe(true);
        }
        expect(screen.text().includes('Repositories:')).toBe(!checkRepositoriesFolder(typed).ok);
        screen.unmount();
      }),
      {numRuns: 40},
    );
  });

  it('keeps the line under each field, so an error never moves what is below', () => {
    const {screen} = open();
    const line = (label: string) => input(screen, label).closest('.vs-field');
    const count = (label: string): number =>
      line(label)?.querySelectorAll(':scope > .vs-help').length ?? 0;
    expect(count('This machine')).toBe(1);
    typeInto(input(screen, 'This machine'), 'nope');
    expect(count('This machine')).toBe(1);
    typeInto(input(screen, 'This machine'), '/ok');
    expect(count('This machine')).toBe(1);
    screen.unmount();
  });
});
