package evaluator

import (
	"context"
	"fmt"
	"io"
	"os"
	"os/signal"
	"regexp"
	"sync/atomic"
	"syscall"
)

// exitUsage is the exit status of a usage error, also for a mode the task does not define.
const exitUsage = 2

const usageText = `usage:
  TASK check                                exit 0 when the check passes
  TASK benchmark --vs-output FILE [--seed N]   one sample, result-protocol-2 records
  TASK profile [--seed N]                   the standard profile load, never scored
`

// Main runs the task script's command (check, benchmark or profile) from os.Args and exits: the
// Go mirror of Python's vibesys_evaluator.main.
//
// `benchmark --vs-output FILE` writes result-protocol-2 records: hello with the metrics, then the
// result the benchmark reports with run.Report or an error record naming what failed; the exit
// status is 0 only for a result. `check` exits 0 when the function returns nil and 1 with the
// reason it returns. `profile` runs the load a profiler records; it is never scored. The first
// SIGTERM, SIGINT or SIGHUP cancels the context the functions receive and ends the sample as an
// error (a benchmark's records say so; check and profile exit 128 plus the signal); later ones
// are ignored, so whatever the sample opened is closed on the way out.
func Main(task Task) {
	sigs := make(chan os.Signal, 4)
	signal.Notify(sigs, syscall.SIGTERM, syscall.SIGINT, syscall.SIGHUP)
	ctx, cancel := context.WithCancel(context.Background())
	var got atomic.Int32
	go func() {
		s := <-sigs
		got.Store(int32(s.(syscall.Signal)))
		cancel()
		for range sigs { // later signals are ignored
		}
	}()
	st := &stopper{ctx: ctx, signal: func() syscall.Signal { return syscall.Signal(got.Load()) }}
	code := execute(st, task, os.Args[1:], os.Getenv, os.Stderr)
	cancel()
	os.Exit(code)
}

var integer = regexp.MustCompile(`^[+-]?[0-9]+$`)

// execute is the whole command, in process so tests drive it directly.
func execute(st *stopper, task Task, args []string, env func(string) string, stderr io.Writer) int {
	usage := func(format string, a ...any) int {
		fmt.Fprintf(stderr, "bench: error: "+format+"\n"+usageText, a...)
		return exitUsage
	}
	if len(args) == 0 {
		return usage("a command is required")
	}
	mode := Mode(args[0])
	if mode != Check && mode != Benchmark && mode != Profile {
		return usage("unknown command %q", args[0])
	}
	var output, seed string
	var hasSeed bool
	rest := args[1:]
	for i := 0; i < len(rest); i++ {
		name, value, hasValue := cutFlag(rest[i])
		take := func() (string, bool) {
			if hasValue {
				return value, true
			}
			if i+1 < len(rest) {
				i++
				return rest[i], true
			}
			return "", false
		}
		switch {
		case name == "--vs-output" && mode == Benchmark:
			v, ok := take()
			if !ok {
				return usage("argument --vs-output: expected one argument")
			}
			output = v
		case name == "--seed" && mode != Check:
			v, ok := take()
			if !ok {
				return usage("argument --seed: expected one argument")
			}
			if !integer.MatchString(v) {
				return usage("argument --seed: invalid int value: %q", v)
			}
			seed, hasSeed = v, true
		default:
			return usage("unrecognized arguments: %s", rest[i])
		}
	}
	switch mode {
	case Benchmark:
		if output == "" {
			return usage("the following arguments are required: --vs-output")
		}
		if task.Benchmark == nil {
			return usage("this task defines no benchmark: set Task.Benchmark")
		}
		return benchmarkSample(st, task, output, seed, hasSeed, env)
	case Check:
		if task.Check == nil {
			Log("this task defines no check: set Task.Check")
			return exitUsage
		}
		return unscoredSample(st, mode, seed, hasSeed, env, task.Check)
	default:
		if task.Profile == nil {
			Log("this task defines no profile: set Task.Profile")
			return exitUsage
		}
		return unscoredSample(st, mode, seed, hasSeed, env, task.Profile)
	}
}

func cutFlag(arg string) (name, value string, hasValue bool) {
	for i := 0; i < len(arg); i++ {
		if arg[i] == '=' {
			return arg[:i], arg[i+1:], true
		}
	}
	return arg, "", false
}
