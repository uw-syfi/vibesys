// Package fakeworld is the envtest.World of the fake machine: the in-memory gates and points the
// fake's scripts signal through, and the renderer from a contract-suite Program to a script. The
// fake environment and the fake docker engine are held to the same contract by running their
// scripts in it.
package fakeworld

import (
	"strconv"

	"github.com/uw-syfi/vibesys/cli/internal/environment/envtest"
	"github.com/uw-syfi/vibesys/cli/internal/environment/fakeenv"
)

// World is an envtest.World whose programs are scripts for a fakeenv.Machine. Its gates and points
// are the in-memory ones the machine waits at.
type World struct{ *fakeenv.MemoryGates }

var _ envtest.World = World{}

// New returns a world with no point reached and no gate released.
func New() World { return World{fakeenv.NewMemoryGates()} }

// Missing implements envtest.World.
func (World) Missing() []string { return []string{"no-such-command"} }

// Argv implements envtest.World.
func (World) Argv(p envtest.Program) []string {
	if len(p) == 0 {
		return []string{"exit", "0"} // a command line cannot be empty
	}
	return render(p)
}

func render(p envtest.Program) []string {
	var out []string
	for i, s := range p {
		if i > 0 {
			out = append(out, ";")
		}
		switch s.Op {
		case envtest.OpPrint:
			out = append(out, "print", s.Text)
		case envtest.OpWarn:
			out = append(out, "warn", s.Text)
		case envtest.OpDir:
			out = append(out, "dir")
		case envtest.OpMark:
			out = append(out, "mark", s.Name)
		case envtest.OpHold:
			out = append(out, "hold", s.Name)
		case envtest.OpExit:
			out = append(out, "exit", strconv.Itoa(s.Code))
		case envtest.OpCrash:
			out = append(out, "crash")
		case envtest.OpChild:
			out = append(out, "child", "{")
			out = append(out, render(s.Body)...)
			out = append(out, "}")
		case envtest.OpAlive:
			out = append(out, "alive", s.Name)
		case envtest.OpUnlessFlag:
			out = append(out, "unless-flag", s.Name)
		default:
			panic("fakeworld: no rendering for " + string(s.Op))
		}
	}
	return out
}
