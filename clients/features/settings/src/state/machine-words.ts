/**
 * What the machine sections of Settings say about what a helper reported: a cluster's reach and
 * hardware, the Modal token, and each harness's login, each in the words a person reads.
 */
import {
  type AgentLogin,
  type ComputeSettings,
  FAILURE_CAUSE_WORDS,
  type FailureCause,
  type SlurmClusterSettings,
} from '@vibesys/ipc';

type Tone = 'neutral' | 'ok' | 'warn' | 'err';

const ACCELERATORS = {
  cuda: 'NVIDIA (CUDA)',
  rocm: 'AMD (ROCm)',
  metal: 'Apple (Metal)',
  cpu: 'CPU only',
} as const;

/** How the machine reaches `cluster`: SSH and its host alias, or the machine is its login node. */
export function reachWords(cluster: SlurmClusterSettings): string {
  return cluster.reach.kind === 'ssh' ? `SSH · ${cluster.reach.host}` : 'Login node';
}

/** What a cluster's nodes offer, or that it is not stated; the GPU models are `hardwareModels`. */
export function hardwareWords(hardware: SlurmClusterSettings['hardware']): string {
  if (hardware === null || hardware === undefined) return 'Not stated';
  const parts: string[] = [];
  const perNode = hardware.gpus_per_node ?? 0;
  if (perNode > 0) parts.push(`${perNode} GPU${perNode === 1 ? '' : 's'} per node`);
  if (hardware.accelerator !== null && hardware.accelerator !== undefined) {
    parts.push(ACCELERATORS[hardware.accelerator]);
  }
  return parts.length === 0 ? 'Not stated' : parts.join(' · ');
}

/** The GPU models a cluster's nodes list, as the scheduler names them. */
export function hardwareModels(hardware: SlurmClusterSettings['hardware']): readonly string[] {
  return hardware?.gpu_models ?? [];
}

/** Whether `modal` has a token on the machine, and where it is held. */
export function modalWords(modal: ComputeSettings['modal']): {
  readonly label: string;
  readonly tone: Tone;
  readonly detail: string;
} {
  if (!modal.token_present) {
    return {
      label: 'No token',
      tone: 'warn',
      detail:
        'Set the Modal token id and secret in the environment VibeSys starts runs from, or add a profile with the Modal command "modal token set", on this machine.',
    };
  }
  return {
    label: 'Token found',
    tone: 'ok',
    detail:
      modal.source === 'profile'
        ? 'Held in a Modal profile on this machine. VibeSys never reads the secret.'
        : 'Held in the environment this machine starts runs from. VibeSys never reads the secret.',
  };
}

/** How a harness is signed in: the words, and the variable or file they name (set as code). */
export interface HowWords {
  readonly text: string;
  readonly identifier: string | null;
}

const NO_HOW: HowWords = {text: '', identifier: null};

function howWords(login: AgentLogin): HowWords {
  const {how} = login;
  if (how === null || how === undefined) return NO_HOW;
  if (how.kind === 'account') return {text: 'Account', identifier: null};
  if (how.variable !== null && how.variable !== undefined) {
    return {text: 'API key', identifier: how.variable};
  }
  if (how.file !== null && how.file !== undefined) {
    return {text: 'Credentials', identifier: how.file};
  }
  return {text: 'API key', identifier: null};
}

/** A harness's login as a chip, with how it is signed in. */
export function loginWords(login: AgentLogin): {
  readonly label: string;
  readonly tone: Tone;
  readonly how: HowWords;
} {
  if (!login.installed) return {label: 'Not installed', tone: 'neutral', how: NO_HOW};
  const how = howWords(login);
  if (login.state === 'signed_in') return {label: 'Signed in', tone: 'ok', how};
  if (login.state === 'signed_out') return {label: 'Not signed in', tone: 'warn', how};
  return {label: 'Cannot tell', tone: 'neutral', how};
}

/** Why Sign in cannot be pressed for `login` on `machine`, or null when it can. */
export function signInBlocked(login: AgentLogin, machine: string): string | null {
  if (!login.installed) return `${login.provider_name} is not installed on ${machine}.`;
  if (login.can_start_login) return null;
  return `${login.provider_name} has no login command to start.`;
}

/** What a started login asks the person to do, as the two things to read off it. */
export interface LoginSteps {
  /** The address to open. */
  readonly link: string;
  /** The code to enter there; null for a page that takes none. */
  readonly code: string | null;
}

// The host words the line (`login_instruction` in `host_agent_logins.py`): `Open <url>` and, when
// the login showed a code, ` and enter the code <code>`.
const LOGIN_LINE = /^Open (\S+)(?: and enter the code (\S.*))?$/;

/**
 * The link and the code of the host's login instruction, or null for a line in any other shape
 * (a host that words it differently): the line is then shown as it came.
 */
export function loginSteps(instruction: string): LoginSteps | null {
  const found = LOGIN_LINE.exec(instruction);
  return found === null ? null : {link: found[1] ?? '', code: found[2] ?? null};
}

/** What the Machines entry of Settings says of the saved machines: how many, and how many need attention. */
export function machinesDetail(saved: number, attention: number): string {
  const count = `${saved} saved`;
  if (attention === 0) return count;
  return `${count} · ${attention} ${attention === 1 ? 'needs' : 'need'} attention`;
}

/**
 * A failed reading's body: a connection failure as its status word with the raw text for the
 * pointer; any other failure in the words it came with.
 */
export function failureWords(failed: {readonly error: string; readonly cause?: FailureCause}): {
  readonly body: string;
  readonly hint?: string;
} {
  return failed.cause === undefined
    ? {body: failed.error}
    : {body: FAILURE_CAUSE_WORDS[failed.cause], hint: failed.error};
}
