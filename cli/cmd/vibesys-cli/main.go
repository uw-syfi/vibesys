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

// Exit codes. They match the contract in the project README: 2 is invalid input, so a caller can
// tell a mistyped command from a failed job, and 3 is a wait that timed out.
const (
	exitOK           = 0
	exitFailed       = 1
	exitInvalidInput = 2
	exitTimeout      = 3
	// exitEndUnknown is a wait that gave up without learning how its jobs ended. It is not a job
	// that failed.
	exitEndUnknown = 4
)

// deps is what a command line is run with. A test supplies the pieces that would reach the
// machine, and the real ones are made by realDeps.
type deps struct {
	stdout, stderr io.Writer
	getenv         func(string) string
	// cwd is the directory the command runs in.
	cwd string
	git gitrepo.Git
	// options says how to reach the daemon. It is called once a command needs one.
	options func() (client.Options, error)
	// process runs the daemon and supervisor processes, which are this same binary.
	process production.Process
	// upgrade makes what `vibesys-cli upgrade` acts on: where releases come from and which binary
	// to replace.
	upgrade func() (upgradeEnv, error)
}

func main() {
	os.Exit(run(os.Args[1:], realDeps(os.Stdout, os.Stderr)))
}

func realDeps(stdout, stderr io.Writer) deps {
	cwd, _ := os.Getwd()
	home, _ := os.UserHomeDir()
	exe, _ := os.Executable()
	p := production.Process{Version: version, Executable: exe, Getenv: os.Getenv, UID: os.Getuid(), Home: home}
	return deps{
		stdout: stdout, stderr: stderr, getenv: os.Getenv, cwd: cwd,
		git: gitrepo.System{}, options: p.ClientOptions, process: p, upgrade: productionUpgrade,
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
	"daemon":     daemonCommand,
	"supervisor": supervisorCommand,
	"upgrade":    upgradeCommand,
}

const usage = `usage: vibesys-cli <command> [flags]

commands:
  submit command <commit> [flags] -- <argv...>   queue a job and print its id
  wait <job>... [--any] [--timeout 30m]          block until the jobs are final
  cancel <job> [--reason text]                   stop a job
  note <job> <text>                              add a note to a job
  jobs [--commit rev] [--state s] [--note-contains text]
  logs <job> [--follow]                          print a job's output
  daemon status|stop|restart                     manage the daemon
  upgrade                                        replace this binary with the newest release

Every job and daemon command takes --json, and --repo, --task, --run, --harness and --agent, which
default to the environment variables VIBESYS_REPO, VIBESYS_TASK, VIBESYS_RUN, VIBESYS_HARNESS and
VIBESYS_AGENT.
Exit codes: 0 ok, 1 failed, 2 invalid input, 3 wait timed out.
`

// run executes one invocation and returns its exit code. It takes its dependencies as parameters
// so a test drives it in process.
func run(args []string, d deps) int {
	if len(args) == 1 && args[0] == "--version" {
		_, _ = fmt.Fprintf(d.stdout, "vibesys-cli %s\n", version)
		return exitOK
	}
	if len(args) == 1 && (args[0] == "--help" || args[0] == "-h" || args[0] == "help") {
		_, _ = fmt.Fprint(d.stdout, usage)
		return exitOK
	}
	command, ok := lookup(args)
	if !ok {
		_, _ = fmt.Fprint(d.stderr, usage)
		return exitInvalidInput
	}
	// A signal ends the command and nothing more: a job it submitted or waits on goes on.
	ctx, stop := signal.NotifyContext(context.Background(), syscall.SIGINT, syscall.SIGTERM)
	defer stop()
	return command(ctx, d, args[1:])
}

func lookup(args []string) (func(context.Context, deps, []string) int, bool) {
	if len(args) == 0 {
		return nil, false
	}
	command, ok := commands[args[0]]
	return command, ok
}
