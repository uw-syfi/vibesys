// Package command runs bounded subprocesses for repoctl's internal adapters.
package command

import (
	"context"
	"fmt"
	"os/exec"
	"strings"
	"time"
)

// Run executes program with args from root and returns its standard output.
func Run(root, program string, args ...string) ([]byte, error) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, program, args...)
	cmd.Dir = root
	out, err := cmd.Output()
	if err != nil {
		if e, ok := err.(*exec.ExitError); ok {
			return nil, fmt.Errorf("%s %s failed: %s", program, strings.Join(args, " "), strings.TrimSpace(string(e.Stderr)))
		}
		return nil, err
	}
	return out, nil
}
