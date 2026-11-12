/**
 * The structured Add machine form. It produces the one safe SSH command the existing machine
 * boundary accepts; main parses and validates that command again before it persists any option.
 * Passwords and tokens are deliberately absent: SSH config or the person's agent owns them.
 */
import {checkRepositoriesFolder} from './repositories-folder.js';
import {parseSshCommand, printSshCommand, type SshTarget, targetFromInput} from './ssh-target.js';

export type MachineConnectionKind = 'ssh-config' | 'direct' | 'command';

export interface MachineDraft {
  readonly kind: MachineConnectionKind;
  readonly alias: string;
  readonly host: string;
  readonly user: string;
  readonly port: string;
  readonly jump: string;
  readonly identityFile: string;
  readonly command: string;
  readonly repositoriesDirectory: string;
}

export const emptyMachineDraft: MachineDraft = {
  kind: 'ssh-config',
  alias: '',
  host: '',
  user: '',
  port: '',
  jump: '',
  identityFile: '',
  command: '',
  repositoriesDirectory: '',
};

type MachineDraftRefusal = {
  readonly ready: false;
  readonly field: keyof MachineDraft;
  readonly reason: string;
};

export type MachineDraftResult =
  | MachineDraftRefusal
  | {
      readonly ready: true;
      readonly input: string;
      readonly target: SshTarget;
      readonly repositoriesDirectory: string;
    };

const refused = (field: keyof MachineDraft, reason: string): MachineDraftRefusal => ({
  ready: false,
  field,
  reason,
});

type TargetResult = MachineDraftRefusal | {readonly ready: true; readonly target: SshTarget};

function parsedTarget(field: keyof MachineDraft, parse: () => SshTarget): TargetResult {
  try {
    return {ready: true, target: parse()};
  } catch (error) {
    return refused(field, error instanceof Error ? error.message : String(error));
  }
}

function withJump(target: SshTarget, jumpInput: string): TargetResult {
  const jump = jumpInput.trim();
  return jump === ''
    ? {ready: true, target}
    : parsedTarget('jump', () => parseSshCommand(printSshCommand({...target, jump})));
}

function withIdentity(target: SshTarget, identityInput: string): TargetResult {
  const identityFile = identityInput.trim();
  return identityFile === ''
    ? {ready: true, target}
    : parsedTarget('identityFile', () =>
        parseSshCommand(printSshCommand({...target, identityFile})),
      );
}

function directTarget(draft: MachineDraft): TargetResult {
  const host = draft.host.trim();
  const user = draft.user.trim();
  if (host === '') return refused('host', 'Host name or address is required.');
  const base = parsedTarget('host', () => targetFromInput(host));
  if (!base.ready) return base;
  let destination = base.target.destination;
  if (user !== '') {
    const named = parsedTarget('user', () => targetFromInput(`${user}@${host}`));
    if (!named.ready) return named;
    destination = named.target.destination;
  }
  const portText = draft.port.trim();
  const port = portText === '' ? undefined : Number(portText);
  if (port !== undefined && (!Number.isInteger(port) || port < 1 || port > 65_535)) {
    return refused('port', 'Port must be a whole number from 1 to 65535.');
  }
  const jump = withJump({destination, ...(port === undefined ? {} : {port})}, draft.jump);
  return jump.ready ? withIdentity(jump.target, draft.identityFile) : jump;
}

function connectionTarget(draft: MachineDraft): TargetResult {
  switch (draft.kind) {
    case 'ssh-config':
      if (draft.alias.trim() === '') return refused('alias', 'SSH host or alias is required.');
      return parsedTarget('alias', () => targetFromInput(draft.alias));
    case 'direct':
      return directTarget(draft);
    case 'command':
      if (draft.command.trim() === '') return refused('command', 'SSH command is required.');
      return parsedTarget('command', () => parseSshCommand(draft.command));
  }
}

/** Validate the visible fields, then return the canonical command and repository folder sent on. */
export function machineDraftResult(draft: MachineDraft): MachineDraftResult {
  const connection = connectionTarget(draft);
  if (!connection.ready) return connection;
  const repositories = checkRepositoriesFolder(draft.repositoriesDirectory);
  if (!repositories.ok) return refused('repositoriesDirectory', repositories.message);
  return {
    ready: true,
    input: printSshCommand(connection.target),
    target: connection.target,
    repositoriesDirectory: repositories.directory,
  };
}
