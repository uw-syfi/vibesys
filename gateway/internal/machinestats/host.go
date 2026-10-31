package machinestats

import (
	"context"
	"errors"
	"fmt"
	"os"
	"os/exec"

	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

// ErrToolNotFound is what Host.Find returns for a tool the host does not have.
var ErrToolNotFound = errors.New("tool not found")

// Host is everything the sampler reads from the machine. Run returns once ctx
// ends, with the context's error. A gateway started over non-login SSH has a
// thin PATH, so Find searches PATH and then the given full paths.
type Host interface {
	ReadFile(path string) ([]byte, error)
	Find(tool string, candidates ...string) (string, error)
	Run(ctx context.Context, path string, args ...string) ([]byte, error)
}

// SystemHost is the machine the gateway runs on.
type SystemHost struct{}

// ReadFile reads a file.
func (SystemHost) ReadFile(path string) ([]byte, error) { return os.ReadFile(path) }

// Find resolves a tool to an executable path.
func (SystemHost) Find(tool string, candidates ...string) (string, error) {
	if path, err := exec.LookPath(tool); err == nil {
		return path, nil
	}
	for _, candidate := range candidates {
		if path, err := exec.LookPath(candidate); err == nil {
			return path, nil
		}
	}
	return "", fmt.Errorf("%s: %w", tool, ErrToolNotFound)
}

// Run runs a tool without a shell, with bounded output.
func (SystemHost) Run(ctx context.Context, path string, args ...string) ([]byte, error) {
	result, err := hostcommand.ExecRunner{}.Run(ctx, hostcommand.Command{Executable: path, Args: args})
	return result.Output, err
}
