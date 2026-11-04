package fakeenv

import (
	"context"
	"fmt"
	"io"
	"path"
	"strconv"
	"strings"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// step is one step of a script: its words, and for `child` the steps it runs in the background.
type step struct {
	words []string
	body  []step
}

// parse splits a script at its ";" arguments and reads a `child { ... }` or `on-term { ... }` block as one step.
// It reports false for braces that do not pair or that do not follow `child` or `on-term`.
func parse(tokens []string) ([]step, bool) {
	steps, _, ok := parseSteps(tokens, false)
	return steps, ok
}

func parseSteps(tokens []string, nested bool) (steps []step, rest []string, ok bool) {
	var cur []string
	flush := func() {
		if len(cur) > 0 {
			steps = append(steps, step{words: cur})
			cur = nil
		}
	}
	for i := 0; i < len(tokens); i++ {
		switch tokens[i] {
		case ";":
			flush()
		case "{":
			if len(cur) != 1 || (cur[0] != "child" && cur[0] != "on-term") {
				return nil, nil, false
			}
			body, after, ok := parseSteps(tokens[i+1:], true)
			if !ok {
				return nil, nil, false
			}
			steps = append(steps, step{words: cur, body: body})
			cur = nil
			tokens, i = after, -1
		case "}":
			if !nested {
				return nil, nil, false
			}
			flush()
			return steps, tokens[i+1:], true
		default:
			cur = append(cur, tokens[i])
		}
	}
	if nested {
		return nil, nil, false
	}
	flush()
	return steps, nil, true
}

// group is the processes of one process group: the leader's goroutine and the children it
// started. Ending the group ends all of them, and waits for them. Terminating it delivers SIGTERM
// to each of them: see signals.
type group struct {
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup

	termCtx context.Context // done once SIGTERM was delivered to the group
	raise   context.CancelFunc
}

func newGroup() *group {
	g := &group{}
	g.ctx, g.cancel = context.WithCancel(context.Background())
	g.termCtx, g.raise = context.WithCancel(g.ctx)
	return g
}

// terminate delivers SIGTERM to every process of the group, and returns at once.
func (g *group) terminate() { g.raise() }

// signals is how one process of a group answers SIGTERM, as a shell does: the default is to die
// with 143, `ignore-term` ignores it, and `on-term { ... }` runs its steps and then ends the
// process with exit code 0, unless they end it first. A child starts with the default, and
// keeps an ignored SIGTERM.
type signals struct {
	ignore bool
	onTerm []step
}

// caught reports whether a SIGTERM has reached the process and it does not ignore it.
func (g *group) caught(sig signals) bool {
	return !sig.ignore && g.termCtx.Err() != nil && g.ctx.Err() == nil
}

func (g *group) end() {
	g.cancel()
	g.wg.Wait()
}

// interpreter runs the scripts of one execution.
type interpreter struct {
	gates Gates
	// dir is the working directory the `dir` step prints.
	dir string
	// files is where the `emit` step writes; nil when the machine has no such files.
	files Files
	// lostOnEnd is the error of a wait that its group ended, and nil for the default, a cancelled
	// command: a machine whose processes die with it cannot say how they ended.
	lostOnEnd error
}

// splitOutput separates the output file a command is told about from the rest of its script: a
// last argument that is an absolute path, after one that is a flag, as a benchmark is run with
// `--vs-output /scratch/sample-0.out`. The `emit` step writes to that file.
func splitOutput(argv []string) (script []string, file string) {
	n := len(argv)
	if n >= 2 && strings.HasPrefix(argv[n-2], "--") && path.IsAbs(argv[n-1]) {
		return argv[:n-2], argv[n-1]
	}
	return argv, ""
}

// run runs the steps as one process of the group g, writing to out, and to file for `emit`. It
// returns when the process ends: by its own steps, or because the group ended.
func (in *interpreter) run(g *group, steps []step, out io.Writer, file string) (core.ExecOutcome, error) {
	return in.runWith(g, steps, out, file, signals{})
}

// runWith is run for a process that starts with the given way of answering SIGTERM.
func (in *interpreter) runWith(g *group, steps []step, out io.Writer, file string, sig signals) (core.ExecOutcome, error) {
	exited := func(code int) (core.ExecOutcome, error) {
		return core.ExecOutcome{Kind: core.ExecExited, ExitCode: code}, nil
	}
	// terminated is how the process ends when SIGTERM reaches it.
	terminated := func() (core.ExecOutcome, error) {
		if sig.onTerm == nil {
			return exited(128 + 15)
		}
		handler := sig
		handler.onTerm, handler.ignore = nil, true // the handler is not interrupted by a second SIGTERM
		out2, err := in.runWith(g, sig.onTerm, out, file, handler)
		if err != nil || (out2.Kind == core.ExecExited && out2.ExitCode != 0) || (out2.Kind != core.ExecExited && out2.Kind != "") {
			return out2, err
		}
		return exited(0)
	}
	points, hasPoints := in.gates.(Points)
	for _, s := range steps {
		if g.caught(sig) {
			return terminated()
		}
		w := s.words
		switch w[0] {
		case "print", "warn":
			if _, err := fmt.Fprintln(out, strings.Join(w[1:], " ")); err != nil {
				return core.ExecOutcome{}, err
			}
		case "emit":
			if in.files == nil || file == "" {
				return exited(127)
			}
			in.files.Put(file, []byte(strings.Join(w[1:], " ")))
		case "dir":
			if _, err := fmt.Fprintln(out, in.dir); err != nil {
				return core.ExecOutcome{}, err
			}
		case "hold":
			if len(w) != 2 {
				return exited(127)
			}
			waitCtx := g.ctx
			if !sig.ignore {
				waitCtx = g.termCtx // a SIGTERM interrupts the wait, as it does a shell's read
			}
			if !in.gates.Wait(waitCtx, w[1]) {
				if g.caught(sig) {
					return terminated()
				}
				if in.lostOnEnd != nil {
					return core.ExecOutcome{}, in.lostOnEnd
				}
				return core.ExecOutcome{Kind: core.ExecCancelled}, nil
			}
		case "mark", "alive", "unless-flag":
			if !hasPoints || len(w) != 2 {
				return exited(127)
			}
			switch w[0] {
			case "mark":
				points.Mark(w[1])
			case "alive":
				defer points.Live(w[1])()
			case "unless-flag":
				if !points.Flag(w[1]) {
					return exited(1)
				}
			}
		case "child":
			body := s.body
			inherited := signals{ignore: sig.ignore}
			g.wg.Go(func() { _, _ = in.runWith(g, body, out, file, inherited) })
		case "on-term":
			sig.onTerm = append([]step{}, s.body...)
		case "ignore-term":
			sig.ignore = true
		case "exit":
			code, err := strconv.Atoi(strings.Join(w[1:], ""))
			if err != nil {
				return exited(127)
			}
			return exited(code)
		case "crash":
			return exited(137)
		case "timeout":
			return core.ExecOutcome{Kind: core.ExecTimedOut}, nil
		case "service-not-ready":
			return core.ExecOutcome{Kind: core.ExecServiceNotReady}, nil
		case "service-exited":
			return core.ExecOutcome{Kind: core.ExecServiceExited}, nil
		case "unknown":
			return core.ExecOutcome{}, errUnknown
		case "fail":
			return core.ExecOutcome{}, &core.Failure{Kind: core.FailureUnavailable, Message: strings.Join(w[1:], " ")}
		default:
			return exited(127)
		}
	}
	return exited(0)
}

var (
	errEndedUnseen = &core.Failure{Kind: core.FailureUnavailable, Message: "fakeenv: the machine ended the command, and cannot say how it ended"}
	errUnknown     = &core.Failure{Kind: core.FailureUnavailable, Message: "fakeenv: the script ended in a way the environment cannot say"}
)

// process is a started group of an execution: it implements environment.Process.
type process struct {
	g      *group
	exited chan environment.Exit
}

var _ environment.Process = (*process)(nil)

// Exited implements environment.Process.
func (p *process) Exited() <-chan environment.Exit { return p.exited }

// Terminate implements environment.Process.
func (p *process) Terminate() { p.g.terminate() }

// Kill implements environment.Process.
func (p *process) Kill() { p.g.end() }

// launcher starts the processes of one Run.
type launcher struct {
	in *interpreter
	wg *sync.WaitGroup

	mu     sync.Mutex
	groups []*group
}

var _ environment.Launcher = (*launcher)(nil)

// Start implements environment.Launcher. The script's own outcomes that are not an exit, such as
// `timeout`, end the process with that outcome as the execution's.
func (l *launcher) Start(argv []string, sink io.Writer) (environment.Process, error) {
	argv, file := splitOutput(argv)
	steps, ok := parse(argv)
	if !ok {
		steps = []step{{words: []string{"malformed"}}} // exits 127, as a shell does for a command it cannot run
	}
	if len(steps) > 0 && steps[0].words[0] == "fail" {
		// A script that fails first is a command that cannot be started at all.
		return nil, &core.Failure{Kind: core.FailureUnavailable, Message: strings.Join(steps[0].words[1:], " ")}
	}
	g := newGroup()
	l.mu.Lock()
	l.groups = append(l.groups, g)
	l.mu.Unlock()
	p := &process{g: g, exited: make(chan environment.Exit, 1)}
	// The leader is counted in its group before it runs, so the first child it starts never adds to
	// a group that ending the group is already waiting on.
	g.wg.Add(1)
	l.wg.Go(func() {
		defer g.wg.Done()
		out, err := l.in.run(g, steps, sink, file)
		p.exited <- toExit(out, err)
	})
	return p, nil
}

// end ends every group the launcher started, and waits for the leaders.
func (l *launcher) end() {
	l.mu.Lock()
	groups := l.groups
	l.mu.Unlock()
	for _, g := range groups {
		g.end()
	}
	l.wg.Wait()
}

// toExit says how a script ended as environment.Exit does.
func toExit(out core.ExecOutcome, err error) environment.Exit {
	switch {
	case err != nil:
		return environment.Exit{Err: err}
	case out.Kind == core.ExecExited:
		return environment.Exit{Code: out.ExitCode}
	default:
		return environment.Exit{Kind: out.Kind}
	}
}
