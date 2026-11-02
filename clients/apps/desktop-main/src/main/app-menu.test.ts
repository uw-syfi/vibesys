import {describe, expect, test} from 'bun:test';
import type {MenuItemConstructorOptions} from 'electron';
import {applicationMenuTemplate} from './app-menu.js';

const PLATFORMS: readonly NodeJS.Platform[] = ['darwin', 'linux', 'win32'];

function items(template: readonly MenuItemConstructorOptions[]): MenuItemConstructorOptions[] {
  return template.flatMap(item => [
    item,
    ...(Array.isArray(item.submenu) ? items(item.submenu) : []),
  ]);
}

/** Choose `item` as a click on it would. */
function choose(item: MenuItemConstructorOptions | undefined): void {
  if (item?.click === undefined) throw new Error('the menu item is missing or does nothing');
  (item.click as () => void)();
}

function build(platform: NodeJS.Platform) {
  const calls: string[] = [];
  const template = applicationMenuTemplate(platform, 'VibeSys', {
    openSettings: () => calls.push('settings'),
  });
  return {calls, all: items(template)};
}

describe('the application menu', () => {
  test('property: on every platform, Settings is one real Command or Control comma item that opens Settings', () => {
    for (const platform of PLATFORMS) {
      const {calls, all} = build(platform);
      const settings = all.filter(item => item.accelerator === 'CmdOrCtrl+,');
      expect(settings.map(item => item.label)).toEqual(['Settings…']);
      expect(settings[0]?.registerAccelerator).not.toBe(false);
      choose(settings[0]);
      expect(calls).toEqual(['settings']);
    }
  });

  test('property: Settings sits in the application menu on a Mac and in File elsewhere', () => {
    for (const platform of PLATFORMS) {
      const template = applicationMenuTemplate(platform, 'VibeSys', {
        openSettings: () => undefined,
      });
      const holder = template.find(
        item =>
          Array.isArray(item.submenu) &&
          item.submenu.some(entry => entry.accelerator === 'CmdOrCtrl+,'),
      );
      expect(holder?.label).toBe(platform === 'darwin' ? 'VibeSys' : 'File');
    }
  });

  test('property: on every platform, About (the app version) is in the menu once', () => {
    for (const platform of PLATFORMS)
      expect(build(platform).all.filter(item => item.role === 'about')).toHaveLength(1);
  });

  test('View has no sidebar toggle: the shell has no sidebar', () => {
    expect(build('darwin').all.map(item => item.label)).not.toContain('Toggle Sidebar');
  });
});
