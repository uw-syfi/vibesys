export const REPOSITORY_VISIBILITIES = ['private', 'public', 'internal'] as const;

export type RepositoryVisibility = (typeof REPOSITORY_VISIBILITIES)[number];

export interface SetupDefaults {
  input_path: string;
  experiment_name: string;
  repository_owner: string | null;
  repository_name: string;
  visibility: RepositoryVisibility;
}

export interface SetupSelection {
  inputPath: string;
  experimentName: string;
  repositoryOwner: string;
  repositoryName: string;
  visibility: string;
}

const REPOSITORY_COMPONENT = /^[A-Za-z0-9_.-]+$/;

export function parseSetupDefaults(text: string): SetupDefaults {
  const value = JSON.parse(text) as Partial<SetupDefaults>;
  if (
    typeof value.input_path !== 'string' ||
    typeof value.experiment_name !== 'string' ||
    (value.repository_owner !== null && typeof value.repository_owner !== 'string') ||
    typeof value.repository_name !== 'string' ||
    !REPOSITORY_VISIBILITIES.includes(value.visibility as RepositoryVisibility)
  ) {
    throw new Error('backend returned invalid interactive setup defaults');
  }
  return value as SetupDefaults;
}

export function validateSetupSelection(selection: SetupSelection): string | undefined {
  if (selection.inputPath.trim().length === 0) return 'Input bundle is required.';
  if (selection.experimentName.trim().length === 0) return 'Experiment name is required.';

  const owner = selection.repositoryOwner.trim();
  if (owner.length === 0) return undefined;
  if (!REPOSITORY_COMPONENT.test(owner)) {
    return 'Repository owner must be one GitHub user or organization name.';
  }
  if (!REPOSITORY_COMPONENT.test(selection.repositoryName.trim())) {
    return 'Repository name may contain letters, numbers, dot, underscore, and hyphen.';
  }
  if (!REPOSITORY_VISIBILITIES.includes(selection.visibility.trim() as RepositoryVisibility)) {
    return 'Visibility must be private, public, or internal.';
  }
  return undefined;
}

export function applySetupSelection(argv: string[], selection: SetupSelection): string[] {
  const result = withoutOptions(argv, ['--input', '--exp-name', '--repo', '--repo-visibility']);
  result.push('--input', selection.inputPath.trim());
  result.push('--exp-name', selection.experimentName.trim());

  const owner = selection.repositoryOwner.trim();
  if (owner.length > 0) {
    result.push('--repo', `${owner}/${selection.repositoryName.trim()}`);
    result.push('--repo-visibility', selection.visibility.trim());
  }
  return result;
}

export function shouldOfferInteractiveSetup(argv: string[]): boolean {
  return !argv.some(
    argument =>
      argument === '--resume' ||
      argument.startsWith('--resume=') ||
      argument === '--repo' ||
      argument.startsWith('--repo=') ||
      argument === '--stub-agent' ||
      argument === '--headless',
  );
}

function withoutOptions(argv: string[], options: string[]): string[] {
  const result: string[] = [];
  for (let index = 0; index < argv.length; index += 1) {
    const argument = argv[index];
    if (argument === undefined) continue;
    const option = options.find(
      candidate => argument === candidate || argument.startsWith(`${candidate}=`),
    );
    if (option === undefined) {
      result.push(argument);
      continue;
    }
    if (argument === option) index += 1;
  }
  return result;
}
