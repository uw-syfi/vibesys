/** The connection manager's `Phase`, as the views word a host connection. */
import type {ConnectionStatus} from '@vibesys/hosts';
import type {Phase} from '../platform/connection/index.js';

/** How a host connection's phase reads in the views. */
export function connectionStatus(phase: Phase): ConnectionStatus {
  switch (phase.kind) {
    case 'idle':
    case 'linking':
    case 'handshaking':
      return {kind: 'connecting'};
    case 'ready':
      return {kind: 'connected'};
    case 'reconnecting':
      return {kind: 'reconnecting', attempt: phase.attempt};
    case 'offline':
      return {kind: 'offline', detail: phase.detail};
    case 'auth-needed':
      return {kind: 'auth-needed', detail: phase.detail};
    case 'incompatible':
      return {kind: 'incompatible', detail: phase.detail};
    case 'closed':
      return {kind: 'closed'};
  }
}
