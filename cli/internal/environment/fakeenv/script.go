package fakeenv

import (
	"context"
	"fmt"
	"io"
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

// parse splits a script at its ";" arguments and reads a `child { ... }` block as one step.
// It reports false for braces that do not pair or that do not follow `child`.
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
			if len(cur) != 1 || cur[0] != "child" {
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
// started. Ending the group ends all of them, and waits for them.
type group struct {
	ctx    context.Context
	cancel context.CancelFunc
	wg     sync.WaitGroup
}

func newGroup() *group {
	g := &group{}
	g.ctx, g.cancel = context.WithCancel(context.Background())
	return g
}

func (g *group) end() {
	g.cancel()
	g.wg.Wait()
}

// interpreter runs the scripts of one execution.
type interpreter struct {
	env *Environment
	x   core.Execution
}

// run runs the steps as one process of the group g, writing to out. It returns when the process
// ends: by its own steps, or because the group ended.
func (in *interpreter) run(g *group, steps []step, out io.Writer) (core.ExecOutcome, error) {
	exited := func(code int) (core.ExecOutcome, error) {
		return core.ExecOutcome{Kind: core.ExecExited, ExitCode: code}, nil
	}
	points, hasPoints := in.env.gates.(Points)
	for _, s := range steps {
		w := s.words
		switch w[0] {
		case "print", "warn":
			if _, err := fmt.Fprintln(out, strings.Join(w[1:], " ")); err != nil {
				return core.ExecOutcome{Kind: core.ExecUnknown}, err
			}
		case "dir":
			if _, err := fmt.Fprintln(out, in.x.Checkout.Dir); err != nil {
				return core.ExecOutcome{Kind: core.ExecUnknown}, err
			}
		case "hold":
			if len(w) != 2 || !in.env.gates.Wait(g.ctx, w[1]) {
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
			g.wg.Go(func() { _, _ = in.run(g, body, out) })
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
			return core.ExecOutcome{Kind: core.ExecUnknown}, nil
		case "fail":
			return core.ExecOutcome{}, &core.Failure{Kind: core.FailureUnavailable, Message: strings.Join(w[1:], " ")}
		default:
			return exited(127)
		}
	}
	return exited(0)
}

// process is a started group of an execution: it implements environment.Process.
type process struct {
	g      *group
	exited chan environment.Exit
}

var _ environment.Process = (*process)(nil)

// Exited implements environment.Process.
func (p *process) Exited() <-chan environment.Exit { return p.exited }

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
	steps, ok := parse(argv)
	if !ok {
		steps = []step{{words: []string{"malformed"}}} // exits 127, as a shell does for a command it cannot run
	}
	g := newGroup()
	l.mu.Lock()
	l.groups = append(l.groups, g)
	l.mu.Unlock()
	p := &process{g: g, exited: make(chan environment.Exit, 1)}
	l.wg.Go(func() {
		out, err := l.in.run(g, steps, sink)
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
