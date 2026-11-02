/**
 * The main process side: a source that reads a remote host's daemon through
 * `ssh <host> vibesys-orchestrator connect`. The renderer takes any `OrchestratorSource`, so this
 * is the one place that knows the host is reached over ssh.
 */
import {type OrchestratorSource, StdioSource} from '@vibesys/orchestrator-source';
import {sshLauncher} from '@vibesys/orchestrator-source/node';

export function connectOverSsh(host: string): OrchestratorSource & {close(): void} {
  return new StdioSource(sshLauncher(host));
}
