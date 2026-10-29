package server

import (
	"errors"
	"os/exec"
	"slices"
	"strings"

	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

// usageExitStatus is argparse's exit status for an unknown command or option.
const usageExitStatus = 2

// exitFailureCategory types a host command that exited without a reply. It is
// UNSUPPORTED only on explicit evidence that the checkout does not know that
// command: the usage exit status, nothing on stdout, and argparse naming the
// command as an invalid choice or an unrecognized argument. The gateway runs
// only commands its own registry defines, so a checkout that lacks one predates
// the gateway, whichever command it is.
func exitFailureCategory(cli string, result hostcommand.Result, err error) hostv1.FailureCategory {
	var exit *exec.ExitError
	if (err == nil || errors.As(err, &exit)) && unknownCommand(cli, result.ExitCode, len(result.Output), result.Stderr) {
		return hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED
	}
	return hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL
}

// unknownCommand reports whether a failed run is argparse rejecting the
// the command cli: its name as an invalid choice, or as a whole token of the
// unrecognized arguments. A longer word, an option or a path that merely
// contains the name is not the command.
func unknownCommand(cli string, exitCode, outputLength int, stderr string) bool {
	if exitCode != usageExitStatus || outputLength != 0 {
		return false
	}
	for _, line := range strings.Split(stderr, "\n") {
		if strings.Contains(line, "invalid choice: '"+cli+"'") {
			return true
		}
		if _, listed, found := strings.Cut(line, "unrecognized arguments:"); found && slices.Contains(strings.Fields(listed), cli) {
			return true
		}
	}
	return false
}
