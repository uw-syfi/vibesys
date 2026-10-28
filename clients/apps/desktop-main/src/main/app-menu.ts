import type {MenuItemConstructorOptions} from 'electron';

/** What the application menu's own items do; the rest are Electron roles. */
export interface MenuActions {
  /** Show Settings in the main window, bringing the window to the front. */
  readonly openSettings: () => void;
}

/**
 * The application menu. Settings holds its accelerator itself, so the shortcut works from any
 * window and focus. About, the one place the app's version shows, is in the application menu on a
 * Mac and in Help elsewhere.
 */
export function applicationMenuTemplate(
  platform: NodeJS.Platform,
  appName: string,
  actions: MenuActions,
): MenuItemConstructorOptions[] {
  const settings: MenuItemConstructorOptions = {
    label: 'Settings…',
    accelerator: 'CmdOrCtrl+,',
    click: actions.openSettings,
  };
  const first: MenuItemConstructorOptions =
    platform === 'darwin'
      ? {
          label: appName,
          submenu: [
            {role: 'about'},
            {type: 'separator'},
            settings,
            {type: 'separator'},
            {role: 'services'},
            {type: 'separator'},
            {role: 'hide'},
            {role: 'hideOthers'},
            {role: 'unhide'},
            {type: 'separator'},
            {role: 'quit'},
          ],
        }
      : {label: 'File', submenu: [settings, {type: 'separator'}, {role: 'quit'}]};
  return [
    first,
    {role: 'editMenu'},
    {
      label: 'View',
      submenu: [
        {role: 'reload'},
        {role: 'forceReload'},
        {role: 'toggleDevTools'},
        {type: 'separator'},
        {role: 'resetZoom'},
        {role: 'zoomIn'},
        {role: 'zoomOut'},
        {type: 'separator'},
        {role: 'togglefullscreen'},
      ],
    },
    {role: 'windowMenu'},
    ...(platform === 'darwin' ? [] : [{label: 'Help', submenu: [{role: 'about' as const}]}]),
  ];
}
