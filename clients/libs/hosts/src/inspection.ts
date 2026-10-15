/** Semantic refinement of a structurally parsed `instances.inspect` reply. */
import type {InstanceInspection} from '@vibesys/contracts/host-helper';

function invalid(detail: string): never {
  throw new Error(`Invalid instances.inspect reply: ${detail}`);
}

function absolute(path: string, field: string): void {
  if (!path.startsWith('/')) invalid(`${field} must be an absolute host path`);
}

function validateRecord(inspection: InstanceInspection): void {
  const record = inspection.record;
  if (record === null) return;
  if (record.id !== inspection.id) {
    invalid(`record id ${record.id} does not match ${inspection.id}`);
  }
  absolute(record.socket_path, 'record.socket_path');
  absolute(record.log_path, 'record.log_path');
  absolute(record.project_root, 'record.project_root');
  if (record.vibesys_root != null) absolute(record.vibesys_root, 'record.vibesys_root');
}

function validateMetadata(inspection: InstanceInspection): void {
  const metadata = inspection.metadata;
  if (metadata === null) return;
  if (metadata.id !== inspection.id) {
    invalid(`metadata id ${metadata.id} does not match ${inspection.id}`);
  }
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
  if (
    inspection.record !== null &&
    (metadata.project_root !== inspection.record.project_root ||
      metadata.run_id !== inspection.record.run_id)
  ) {
    invalid('record and retained metadata name different runs');
  }
}

/**
 * The app never asks for the `server.log` tail (`include_log`, deprecated): a run's logs come from
 * its log store. A reply that carries one anyway answers a request the app did not make.
 */
function validateLog(inspection: InstanceInspection): void {
  if (inspection.log != null) invalid('it carries a server log the app did not ask for');
}

/**
 * Refine invariants that JSON Schema cannot express before inspection data crosses into renderer
 * IPC. The helper's generated schema already owns structural validation.
 */
export function inspectionReply(
  requestedId: string,
  inspection: InstanceInspection,
): InstanceInspection {
  if (inspection.id !== requestedId) {
    invalid(`requested ${requestedId}, received ${inspection.id}`);
  }
  if (inspection.state === 'live' && inspection.record === null) {
    invalid('a live inspection requires a record');
  }

  absolute(inspection.control_socket_path, 'control_socket_path');
  absolute(inspection.server_log_path, 'server_log_path');
  validateRecord(inspection);
  validateMetadata(inspection);
  validateLog(inspection);
  return inspection;
}
