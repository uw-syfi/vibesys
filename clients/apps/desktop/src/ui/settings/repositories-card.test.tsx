import {describe, expect, it} from '@effect/vitest';
import fc from 'fast-check';
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

function open(options: {readonly refuse?: string} = {}) {
  const saved: Saved[] = [];
  const repositories: RepositoriesControls = {
    rows: [
      {key: 'local', label: 'This machine', folder: ''},
      {key: 'ssh:host-a', label: 'host-a', folder: '~/work'},
    ],
    save: (key, folder, done) => {
      saved.push({key, folder});
      done(options.refuse ?? null);
    },
  };
  const screen = mount(
    <SettingsScreen
      appearance={initialAppearance('light')}
      choose={() => undefined}
      repositories={repositories}
    />,
  );
  return {screen, saved};
}

const input = (screen: Screen, label: string) => fieldOf<HTMLInputElement>(screen, label);
const save = (screen: Screen, label: string): HTMLElement => {
  const field = input(screen, label).closest('.vs-field');
  const button = [...(field?.querySelectorAll<HTMLElement>('button') ?? [])].find(
    each => each.textContent?.trim() === 'Save',
  );
  if (button === undefined) throw new Error(`no Save beside ${label}`);
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
