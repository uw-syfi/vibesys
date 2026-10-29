const referenceBrand: unique symbol = Symbol('RunReference');

/** An addressed run; only the validating constructor can supply the project path. */
export interface RunReference {
  readonly [referenceBrand]: true;
  readonly projectRoot: string;
  readonly runId: string;
}

export function runReference(projectRoot: string, runId: string): RunReference {
  if (projectRoot.trim() === '') throw new Error('run.projectRoot is required');
  if (runId.trim() === '') throw new Error('run.runId is required');
  return {[referenceBrand]: true, projectRoot, runId};
}
