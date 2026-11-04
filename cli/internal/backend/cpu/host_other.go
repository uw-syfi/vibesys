//go:build !linux && !darwin

package cpu

import (
	"context"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Host returns a System that says this operating system is not supported.
func Host() System { return unsupported{} }

type unsupported struct{}

func (unsupported) Machine(context.Context) (core.MachineFacts, error) {
	return core.MachineFacts{}, unavailable("the CPU backend reads Linux and macOS only")
}

func (unsupported) Read(context.Context) (Reading, error) {
	return Reading{}, unavailable("the CPU backend reads Linux and macOS only")
}
