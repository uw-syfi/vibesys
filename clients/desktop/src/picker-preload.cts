/**
 * Sandboxed preload of the host and run picker window: the page's only way out of its sandbox.
 *
 * It exposes one function per picker request (`picker-protocol.ts`); each is an `invoke` the main
 * process answers only for picker windows. Sandboxed preloads cannot import local files, hence the
 * channel names repeated here; the types keep them in step.
 */
import electron = require('electron');
import type {HostKey, PickerHost, PickerResult, PickerRun} from './picker-protocol.js';

type Channels = typeof import('./picker-protocol.js').PICKER_CHANNELS;

const CHANNELS: Channels = {
  hosts: 'vibesys:picker:hosts',
  runs: 'vibesys:picker:runs',
  signIn: 'vibesys:picker:sign-in',
  attach: 'vibesys:picker:attach',
  start: 'vibesys:picker:start',
  resume: 'vibesys:picker:resume',
  saveSettings: 'vibesys:picker:save-settings',
};

function call<T>(channel: string, ...args: unknown[]): Promise<PickerResult<T>> {
  return electron.ipcRenderer.invoke(channel, ...args) as Promise<PickerResult<T>>;
}

electron.contextBridge.exposeInMainWorld('vibesysPicker', {
  hosts: () => call<PickerHost[]>(CHANNELS.hosts),
  runs: (host: HostKey) => call<PickerRun[]>(CHANNELS.runs, host),
  signIn: (host: HostKey) => call<null>(CHANNELS.signIn, host),
  attach: (host: HostKey, instance: string) => call<null>(CHANNELS.attach, host, instance),
  start: (host: HostKey, project: string, args: string) =>
    call<null>(CHANNELS.start, host, project, args),
  resume: (host: HostKey, project: string, run: string) =>
    call<null>(CHANNELS.resume, host, project, run),
  saveSettings: (host: HostKey, vibesysCommand: string, pythonCommand: string) =>
    call<null>(CHANNELS.saveSettings, host, vibesysCommand, pythonCommand),
});
