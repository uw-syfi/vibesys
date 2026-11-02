package fakeenv

import (
	"io"
	"sync"

	"github.com/uw-syfi/vibesys/cli/internal/environment"
)

// Machine is a fake machine that runs scripts as processes: the place an environment with its own
// machine in it, such as a fake container, runs its commands. Every process it starts is a
// goroutine of a process group of its own, and End ends them all, so nothing outlives the machine.
// A command that End or Kill ends while it waits reports an Exit whose Err is the machine's lost
// error: a machine that is gone cannot say how its commands ended.
//
// A Machine is an environment.Launcher, so environment.Execute drives it as it drives a shell.
type Machine struct {
	l  *launcher
	wg *sync.WaitGroup
}

var _ environment.Launcher = (*Machine)(nil)

// NewMachine returns a machine whose scripts wait at the gates and print dir for the `dir` step.
// lost is the error of a command that End or Kill ended, so that an engine's fake reports it the
// way the engine does; nil gives an error of fakeenv's own.
func NewMachine(gates Gates, dir string, lost error) *Machine {
	if lost == nil {
		lost = errEndedUnseen
	}
	wg := &sync.WaitGroup{}
	return &Machine{l: &launcher{in: &interpreter{gates: gates, dir: dir, lostOnEnd: lost}, wg: wg}, wg: wg}
}

// Start implements environment.Launcher. The sink is written to by several processes at once, so
// it must be safe for concurrent use.
func (m *Machine) Start(argv []string, sink io.Writer) (environment.Process, error) {
	return m.l.Start(argv, sink)
}

// End ends every process group the machine started and waits for them. It is safe to call twice.
func (m *Machine) End() { m.l.end() }
