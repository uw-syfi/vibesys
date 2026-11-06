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
// directory. The names it reads are listed, with who sets them, in the identity golden
// (proto/vibesys/cli/v1/conformance/identity_env.golden.json), which a test holds this to. The
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
	"hold":       holdCommand,
	"release":    releaseCommand,
	"jobs":       jobsCommand,
	"logs":       logsCommand,
	"show":       showCommand,
	"compare":    compareCommand,
	"promote":    promoteCommand,
	"result":     resultCommand,
	"frontier":   frontierCommand,
	"plan":       planCommand,
	"daemon":     daemonCommand,
	"supervisor": supervisorCommand,
	// Run in a container by the docker environment, never by a person: see probeCommand.
	"probe-ready": probeCommand,
	"upgrade":     upgradeCommand,
}

const usage = `usage: vibesys-cli <command> [flags]

commands:
  submit check <commit> [flags]                  queue the task's check and print the job
  submit measure <commit> [--samples n] [--recheck] [flags]
                                                 queue the task's benchmark and print the job
  submit command <commit> [--wrap prefix] [flags] -- <argv...>
                                                 queue a command and print the job
  submit profile <commit> [--wrap prefix]        queue the task's [profile] load against the
                                                 candidate's server started under the prefix
  wait <job>... [--any] [--timeout 30m]          block until the jobs are final
  cancel <job> [--reason text]                   stop a job
  note <job> <text>                              add a note to a job
  hold <job> [--reason text]                     keep a queued job from starting; it keeps its place
  release <job>                                  let a held job start in its turn
  jobs [--commit rev] [--state s] [--note-contains text]
  logs <job> [--follow]                          print a job's output
  show <commit>                                  results saved for a commit
  compare <commit> [--against rev]               better, worse, within_noise, tradeoff or inconclusive
  promote <commit>                               move the result branch, or say why not
  result                                         the head and a fresh final number
  frontier add <commit>                          add a multi-objective commit to the frontier, or say why not
  frontier list                                  the frontier, with each member's numbers
  plan create|list|view|edit|comment|close       store and find plans
  daemon status|stop|restart                     manage the daemon
  daemon drain [--reason text] | daemon resume   start no queued job until resumed, across restarts
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
