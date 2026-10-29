/** Validated protobuf persistence; UI-facing values stay projections owned by settings. */
import {create, toBinary} from '@bufbuild/protobuf';
import type {DesktopRunStart} from '@vibesys/proto';
import {
  DesktopAppearanceMode,
  DesktopHostsSchema,
  DesktopPreferencesSchema,
  DesktopRecentSchema,
  DesktopTheme,
  RetainedDesktopHostsSchema,
  StartRunReasoning,
  StartRunResults,
  validatedExactFromBinary,
} from '@vibesys/proto';
import type {RunStart} from '@vibesys/runs';
import type {ThemeName} from '@vibesys/ui/tokens';
import {parseSettings, type SettingsFile} from './host-settings.js';
import {type Preferences, parsePreferences} from './preferences.js';
import {type ParsedRecent, parseRecent, recentDocument} from './recent.js';

const MODES = {
  system: DesktopAppearanceMode.SYSTEM,
  light: DesktopAppearanceMode.LIGHT,
  dark: DesktopAppearanceMode.DARK,
} as const;
const THEMES: Readonly<Record<ThemeName, number>> = {
  paper: DesktopTheme.PAPER,
  mist: DesktopTheme.MIST,
  sand: DesktopTheme.SAND,
  ink: DesktopTheme.INK,
  midnight: DesktopTheme.MIDNIGHT,
  terminal: DesktopTheme.TERMINAL,
};
const REASONING = {
  default: StartRunReasoning.DEFAULT,
  low: StartRunReasoning.LOW,
  medium: StartRunReasoning.MEDIUM,
  high: StartRunReasoning.HIGH,
} as const;

/** The protobuf sibling of a retained JSON path. The JSON path is never written again. */
export function settingsBinaryPath(legacyPath: string): string {
  return legacyPath.endsWith('.json') ? `${legacyPath.slice(0, -5)}.pb` : `${legacyPath}.pb`;
}

export function encodeSettings(value: SettingsFile): Uint8Array {
  const parsed = parseSettings(value);
  const message = create(DesktopHostsSchema, {
    version: 1,
    hosts: Object.fromEntries(Object.entries(parsed.hosts).map(([key, host]) => [key, {...host}])),
  });
  return checked(DesktopHostsSchema, toBinary(DesktopHostsSchema, message));
}

export function decodeSettings(bytes: Uint8Array): SettingsFile {
  const message = validatedExactFromBinary(RetainedDesktopHostsSchema, bytes);
  return parseSettings({
    version: 2,
    hosts: Object.fromEntries(
      Object.entries(message.hosts).map(([key, host]) => [
        key,
        {
          checkout: host.checkout,
        },
      ]),
    ),
  });
}

export function encodePreferences(value: Preferences): Uint8Array {
  const parsed = parsePreferences(value);
  const message = create(DesktopPreferencesSchema, {
    version: 1,
    appearance: {
      mode: MODES[parsed.appearance.mode],
      light: THEMES[parsed.appearance.light],
      dark: THEMES[parsed.appearance.dark],
    },
    notificationsEnabled: parsed.notifications.enabled,
    archived: parsed.archived.map(run => ({...run})),
  });
  return checked(DesktopPreferencesSchema, toBinary(DesktopPreferencesSchema, message));
}

export function decodePreferences(bytes: Uint8Array): Preferences {
  const message = validatedExactFromBinary(DesktopPreferencesSchema, bytes);
  const appearance = message.appearance;
  if (appearance === undefined) throw new Error('appearance is required');
  return parsePreferences({
    version: 1,
    appearance: {
      mode: keyFor(MODES, appearance.mode),
      light: keyFor(THEMES, appearance.light),
      dark: keyFor(THEMES, appearance.dark),
    },
    notifications: {enabled: message.notificationsEnabled},
    archived: message.archived.map(run => ({host: run.host, runId: run.runId})),
  });
}

export function encodeRecent(value: ParsedRecent): Uint8Array {
  const current = parseRecent(value.file);
  if (current.invalid.length > 0) throw new Error(current.skipped[0]?.reason);
  const message = create(DesktopRecentSchema, {
    version: 1,
    runs: current.file.runs.map(run => ({
      run: {host: run.host, runId: run.runId},
      project: run.project,
      ...(run.task === null ? {} : {task: run.task}),
      ...(run.instanceId === null ? {} : {instanceId: run.instanceId}),
      attachedAt: run.attachedAt,
      ...(run.start === undefined ? {} : {start: startMessage(run.start)}),
    })),
    retained: value.invalid.map(entry => ({
      after: entry.after,
      originalJson: new TextEncoder().encode(JSON.stringify(entry.raw)),
    })),
  });
  return checked(DesktopRecentSchema, toBinary(DesktopRecentSchema, message));
}

export function decodeRecent(bytes: Uint8Array): ParsedRecent {
  const message = validatedExactFromBinary(DesktopRecentSchema, bytes);
  const runs = message.runs.map(run => ({
    host: run.run?.host,
    runId: run.run?.runId,
    project: run.project,
    task: run.task ?? null,
    instanceId: run.instanceId ?? null,
    attachedAt: run.attachedAt,
    ...(run.start === undefined ? {} : {start: startValue(run.start)}),
  }));
  const invalid = message.retained.map(entry => ({
    after: entry.after,
    raw: JSON.parse(new TextDecoder('utf-8', {fatal: true}).decode(entry.originalJson)) as unknown,
  }));
  // Only explicitly retained historical evidence is decoded as JSON. Current
  // settings fields are always typed protobuf values.
  const current = parseRecent({version: 4, runs});
  if (current.invalid.length > 0) throw new Error(current.skipped[0]?.reason);
  return parseRecent(recentDocument(current.file, invalid));
}

function startMessage(start: RunStart) {
  return {
    ...(start.harness === null ? {} : {harness: {...start.harness}}),
    options: {
      ...(start.options.rounds == null ? {} : {rounds: start.options.rounds}),
      ...(start.options.time_limit_s == null ? {} : {timeLimitS: start.options.time_limit_s}),
      results:
        start.options.results === 'keep'
          ? StartRunResults.KEEP
          : start.options.results === 'publish_github'
            ? StartRunResults.PUBLISH_GITHUB
            : StartRunResults.UNSPECIFIED,
      roles: (start.options.roles ?? []).map(role => ({
        role: role.role,
        model: role.model ?? '',
        reasoning: REASONING[role.reasoning_effort ?? 'default'],
      })),
    },
    agents: Object.entries(start.agents).map(([role, harness]) => ({role, harness: {...harness}})),
    measureOn: {
      target:
        start.measure_on.kind === 'slurm'
          ? {case: 'slurmCluster' as const, value: start.measure_on.cluster}
          : {
              case:
                start.measure_on.kind === 'modal' ? ('modal' as const) : ('sameMachine' as const),
              value: {},
            },
    },
    args: [...start.args],
  };
}

function startValue(start: DesktopRunStart) {
  const target = start.measureOn?.target;
  return {
    harness:
      start.harness === undefined
        ? null
        : {provider: start.harness.provider, model: start.harness.model},
    options: {
      rounds: start.options?.rounds ?? null,
      time_limit_s: start.options?.timeLimitS ?? null,
      results:
        start.options?.results === StartRunResults.KEEP
          ? 'keep'
          : start.options?.results === StartRunResults.PUBLISH_GITHUB
            ? 'publish_github'
            : null,
      roles: (start.options?.roles ?? []).map(role => ({
        role: role.role,
        model: role.model || null,
        reasoning_effort: keyFor(REASONING, role.reasoning),
      })),
    },
    agents: Object.fromEntries(
      start.agents.map(agent => [
        agent.role,
        {
          provider: agent.harness?.provider,
          model: agent.harness?.model,
        },
      ]),
    ),
    measure_on:
      target?.case === 'slurmCluster'
        ? {kind: 'slurm', cluster: target.value}
        : {kind: target?.case === 'modal' ? 'modal' : 'same_machine'},
    args: start.args,
  };
}

function keyFor<T extends string>(values: Readonly<Record<T, number>>, value: number): T {
  const key = (Object.keys(values) as T[]).find(key => values[key] === value);
  if (key === undefined) throw new Error(`unknown enum value: ${value}`);
  return key;
}

function checked<D extends Parameters<typeof validatedExactFromBinary>[0]>(
  schema: D,
  bytes: Uint8Array,
): Uint8Array {
  validatedExactFromBinary(schema, bytes);
  return bytes;
}
