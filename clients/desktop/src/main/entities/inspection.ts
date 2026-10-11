/** Semantic refinement of a structurally parsed `instances.inspect` reply. */
import {
  INSTANCE_LOG_TAIL_BYTES,
  type InstanceInspection,
} from '@vibesys/backend-client/host-helper';
import type {WelcomeInspection} from '../../common/welcome-protocol.js';

function invalid(detail: string): never {
  throw new Error(`Invalid instances.inspect reply: ${detail}`);
}

function absolute(path: string, field: string): void {
  if (!path.startsWith('/')) invalid(`${field} must be an absolute host path`);
}

/**
 * Refine invariants that JSON Schema cannot express before inspection data crosses into renderer
 * IPC. The helper's generated schema already owns structural validation.
 */
export function inspectionReply(
  requestedId: string,
  inspection: InstanceInspection,
): WelcomeInspection {
  if (inspection.id !== requestedId) {
    invalid(`requested ${requestedId}, received ${inspection.id}`);
  }
  if (inspection.record !== null && inspection.record.id !== inspection.id) {
    invalid(`record id ${inspection.record.id} does not match ${inspection.id}`);
  }
  if (inspection.metadata !== null && inspection.metadata.id !== inspection.id) {
    invalid(`metadata id ${inspection.metadata.id} does not match ${inspection.id}`);
  }
  if (inspection.state === 'live' && inspection.record === null) {
    invalid('a live inspection requires a record');
  }

  absolute(inspection.control_socket_path, 'control_socket_path');
  absolute(inspection.server_log_path, 'server_log_path');
  const record = inspection.record;
  if (record !== null) {
    absolute(record.socket_path, 'record.socket_path');
    absolute(record.log_path, 'record.log_path');
    absolute(record.project_root, 'record.project_root');
    if (record.vibesys_root != null) absolute(record.vibesys_root, 'record.vibesys_root');
  }
  const metadata = inspection.metadata;
  if (metadata !== null) {
    absolute(metadata.project_root, 'metadata.project_root');
    absolute(metadata.control_socket_path, 'metadata.control_socket_path');
    absolute(metadata.server_log_path, 'metadata.server_log_path');
    if (metadata.vibesys_root != null) absolute(metadata.vibesys_root, 'metadata.vibesys_root');
    if (
      metadata.control_socket_path !== inspection.control_socket_path ||
      metadata.server_log_path !== inspection.server_log_path
    ) {
      invalid('top-level paths do not match retained metadata');
    }
  }
  const log = inspection.log;
  if (
    log?.state === 'available' &&
    Buffer.byteLength(log.text, 'utf8') > INSTANCE_LOG_TAIL_BYTES
  ) {
    invalid(`log tail exceeds ${INSTANCE_LOG_TAIL_BYTES} UTF-8 bytes`);
  }
  return inspection;
}
