// Command vibesys-cli is the tool layer the VibeSys optimization skills call for anything that must
// be deterministic: measuring, comparing, promoting and job lifecycle.
//
// This package owns argument parsing and output only. Behavior lives under internal/: the daemon
// holds the queue and the jobs, and a command here reads its flags, makes one or two calls to the
// daemon through internal/client, and prints what came back.
package main

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"syscall"

	"github.com/uw-syfi/vibesys/cli/internal/client"
	"github.com/uw-syfi/vibesys/cli/internal/gitrepo"
	"github.com/uw-syfi/vibesys/cli/internal/production"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// version is the release version. Release builds set it with
// -ldflags "-X main.version=<version>"; any other build reports "dev".
var version = "dev"

// deps is what a command line is run with. A test supplies the pieces that would reach the
// machine, and the real ones are made by realDeps.
type deps struct {
	stdout, stderr io.Writer
	getenv         func(string) string
	// cwd is the directory the command runs in.
	cwd string
	git gitrepo.Revisions
	// stdin and readFile are where a command reads text given as "-" or as a file.
	stdin    io.Reader
	readFile func(string) ([]byte, error)
	// options says how to reach the daemon. It is called once a command needs one.
	options func() (client.Options, error)
	// process runs the daemon and supervisor processes, which are this same binary.
	process production.Process
	// upgrade makes what `vibesys-cli upgrade` acts on: where releases come from and which binary
	// to replace.
	upgrade func() (upgradeEnv, error)
	// jsonErrors says the command line asks for JSON, so that even an error from before its flags
	// are read is a Failure.
	jsonErrors bool
}

func main() {
	os.Exit(run(os.Args[1:], realDeps(os.Stdout, os.Stderr)))
}

func realDeps(stdout, stderr io.Writer) deps { return depsFrom(stdout, stderr, os.Getenv) }

// depsFrom makes the real dependencies over getenv, which is the one way the binary reads its
// environment: the context flags' defaults, the places of its daemon and store, and the home
// directory. The context names are the env_name of the fields of AgentContext and OperatorContext
// (proto/vibesys/cli/v1/env.proto), read by envcodec, and a test holds the binary to exactly those. The
// daemon also takes the whole environment, through os.Environ, only to report what its jobs inherit;
// it decides nothing by it.
func depsFrom(stdout, stderr io.Writer, getenv func(string) string) deps {
	cwd, _ := os.Getwd()
	// The home directory is $HOME, as os.UserHomeDir finds it on Unix, read through getenv.
	home := getenv("HOME")
	exe, _ := os.Executable()
	p := production.Process{Version: version, Executable: exe, Getenv: getenv, Environ: os.Environ, UID: os.Getuid(), GID: os.Getgid(), Home: home}
	return deps{
		stdout: stdout, stderr: stderr, getenv: getenv, cwd: cwd,
		git: gitrepo.System{}, options: p.ClientOptions, process: p, upgrade: productionUpgrade,
		stdin: os.Stdin, readFile: os.ReadFile,
	}
}

// commands lists every command by name. The first argument selects one.
var commands = map[string]func(context.Context, deps, []string) int{
	"submit":     submitCommand,
	"wait":       waitCommand,
	"cancel":     cancelCommand,
	"note":       noteCommand,
	"jobs":       jobsCommand,
	"logs":       logsCommand,
	"show":       showCommand,
	"compare":    compareCommand,
	"promote":    promoteCommand,
	"result":     resultCommand,
	"frontier":   frontierCommand,
	"plan":       planCommand,
	"bound":      boundCommand,
	"workstream": workstreamCommand,
	"alloc":      allocCommand,
	"placement":  placementCommand,
	"task":       taskCommand,
	"daemon":     daemonCommand,
	"supervisor": supervisorCommand,
	// Run in a container by the docker environment, never by a person: see probeCommand.
	"probe-ready": probeCommand,
	"upgrade":     upgradeCommand,
}

const usage = `usage: vibesys-cli <command> [flags]

commands:
  submit check <commit> [flags]                  queue the task's check and print the job
  submit measure <commit> [--samples n] [--recheck] [--plan n | --no-plan] [flags]
                                                 queue the task's benchmark and print the job
  submit command <commit> --image harness|candidate [--wrap prefix] [flags] -- <argv...>
                                                 queue a command in the task's image (harness:
                                                 where the check and benchmark run, beside the
                                                 candidate's server, which --wrap wraps, on a task
                                                 with [candidate]) or in the candidate's image, and
                                                 print the job
  submit ... --alloc <id>|any                    run the job in a standing allocation of the run
  wait <job>... [--any] [--timeout 30m]          block until the jobs are final
  cancel <job> [--reason text]                   stop a job
  note <job> <text>                              add a note to a job
  jobs [--commit rev] [--state s] [--note-contains text]
  logs <job> [--follow]                          print a job's output
  show <commit>                                  results saved for a commit
  compare <commit> [--against rev]               better, worse, within_noise, tradeoff or inconclusive
  promote <commit>                               move the result branch, or say why not
  result                                         the head and a fresh final number
  frontier add <commit>                          add a multi-objective commit to the frontier, or say why not
  frontier list                                  the frontier, with each member's numbers
  plan create|list|view|edit|comment|close       store and find plans
  plan check-gain --under <share>                say whether every open or parked plan's gain bound is below it
  workstream start <title>                       start a line of work plans join, or find the open one with
                                                 that title
  workstream list [--status s]                   the task's workstreams
  workstream edit <n> [--title t] [--status s] [--note text]
  bound declare --objective <name> --value <v> --unit <u> --note <how derived> [--commit rev]
                                                 state the best an objective can reach; a revision is
                                                 a new entry, and nothing is replaced
  bound list [--objective <name>]                every declared bound, oldest first, the current one marked
  task card <task> [--revision rev | --working-tree | --folder dir]
                                                 print what a client shows of a task: domain,
                                                 hardware, model, objectives and heading
  alloc request <placement> --for <duration>     ask for a standing allocation of a placement
  alloc ls [--ended] [--all-runs]                the run's allocations
  alloc show <id>                                one allocation
  alloc wait <id> --until running|ending-in <duration>|ended [--timeout 30m]
                                                 block until it is in that state
  alloc end <id> [--after-jobs]                  end an allocation, now or once its jobs are done
  alloc renew <id> --lead <d> --for <d> --until <time>|run-end
                                                 ask for a successor once its time left is at most the
                                                 lead; alloc renew <id> --off takes the order away
  alloc end-run                                  end every allocation of the run, as its end does
  placement capacity <placement> [--run r]      what the daemon measured of a placement's capacity:
                                                 slots, a cluster's nodes and reservations, recent waits
  daemon status|stop|restart [--force]           manage the daemon
  upgrade                                        replace this binary with the newest release

Every job and daemon command takes --json, and --repo, --task, --run, --harness, --agent and
--placement, which default to the environment variables VIBESYS_REPO, VIBESYS_TASK, VIBESYS_RUN,
VIBESYS_HARNESS, VIBESYS_AGENT and VIBESYS_PLACEMENT. --placement is the one placement of the
operator config a docker job a submit queues may run on, and other commands ignore it. Unset, a job
may run on any that gives what the task needs, and the daemon chooses the least loaded when it
starts.
Exit codes: 0 yes, 1 the answer is no (a job did not succeed, a refusal), 2 invalid input or a
thing that does not exist, 3 a wait timed out, 4 no answer (no daemon, an internal failure, or an
answer this build cannot read). The README lists every failure kind and wait outcome.
`

// run executes one invocation and returns its exit code. It takes its dependencies as parameters
// so a test drives it in process.
func run(args []string, d deps) int {
	if len(args) == 1 && args[0] == "--version" {
		_, _ = fmt.Fprintln(d.stdout, versionLine())
		return exitOK
	}
	if len(args) == 2 && args[0] == "--version" && args[1] == "--json" {
		// What this build speaks, typed, for a program that decides whether it can use this tool
		// without starting a daemon: the message `daemon ensure --json` carries for the daemon.
		printJSON(d, wire.BuildVersions(version, production.CurrentVersions()))
		return exitOK
	}
	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h" || args[0] == "help") {
		_, _ = fmt.Fprint(d.stdout, usage)
		return exitOK
	}
	d.jsonErrors = wantsJSON(args)
	command, ok := lookup(args)
	if !ok {
		if d.jsonErrors {
			return fail(d, true, usagef("%q is not a command", args[0]))
		}
		_, _ = fmt.Fprint(d.stderr, usage)
		return exitInvalidInput
	}
	// A signal ends the command and nothing more: a job it submitted or waits on goes on.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return command(ctx, d, args[1:])
}

// versionLine is what --version prints: the release, and the task schema versions this build reads
// (a manifest's `version`), which follow their own numbers.
func versionLine() string {
	return fmt.Sprintf("vibesys-cli %s (task schemas %s)", version, production.TaskSchemas())
}

func lookup(args []string) (func(context.Context, deps, []string) int, bool) {
	if len(args) == 0 {
		return nil, false
	}
	command, ok := commands[args[0]]
	return command, ok
}
