/** Saved-run metadata discovery; run facts still come from the canonical record reader. */
import {timestampDate} from '@bufbuild/protobuf/wkt';
import type {HostId, ListedRun} from '@vibesys/hosts';
import {acceptListSavedRuns, callListSavedRuns, SavedRunImport} from '@vibesys/proto';
import {type RegistryReadPorts, registryTransport, requireCheckout} from './registry-reads.js';

export interface RegistrySavedRuns {
  list(host: HostId, project: string, signal: AbortSignal): Promise<readonly ListedRun[]>;
}

export function registrySavedRuns(ports: RegistryReadPorts): RegistrySavedRuns {
  return {
    async list(host, project, signal) {
      const checkout = requireCheckout(await ports.checkout(host), null);
      const transport = registryTransport(checkout, (bytes, owned) =>
        ports.unary(host, bytes, owned),
      );
      const runs: ListedRun[] = [];
      let offset = 0;
      let revision = '';
      for (;;) {
        const page = await callListSavedRuns(
          transport,
          acceptListSavedRuns({requestId: 'saved-runs', project, offset, revision}),
          signal,
        );
        if (revision !== '' && page.revision !== revision)
          throw new Error('saved-run catalog revision changed');
        revision = page.revision;
        runs.push(
          ...page.runs.map(run => {
            if (!run.createdAt) throw new Error('saved run has no creation timestamp');
            return {
              runId: run.runId,
              name: run.displayName,
              task: run.task ?? null,
              createdAt: timestampDate(run.createdAt).getTime(),
              ...(run.imported === SavedRunImport.UNAVAILABLE ? {unavailable: run.detail} : {}),
            };
          }),
        );
        if (page.nextOffset === undefined) break;
        if (page.nextOffset <= offset) throw new Error('saved-run catalog cursor did not advance');
        offset = page.nextOffset;
      }
      return runs.sort(
        (left, right) => right.createdAt - left.createdAt || right.runId.localeCompare(left.runId),
      );
    },
  };
}
