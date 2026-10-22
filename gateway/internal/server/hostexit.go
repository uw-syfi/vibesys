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

// versionedCommands are the host commands whose absence from a checkout means
// the checkout predates this gateway: the version handshake itself, which every
// supported checkout provides. Any other command that exits with the usage
// status is an ordinary failure with its own diagnostic.
var versionedCommands = map[string]bool{"inspect-candidate": true}

// exitFailureCategory types a host command that exited without a reply. It is
// UNSUPPORTED only on explicit evidence that the checkout does not know a
// versioned command: the usage exit status, nothing on stdout, and argparse
// naming that command as an invalid choice or an unrecognized argument.
func exitFailureCategory(cli string, result hostcommand.Result, err error) hostv1.FailureCategory {
	var exit *exec.ExitError
	if (err == nil || errors.As(err, &exit)) && unknownCommand(cli, result.ExitCode, len(result.Output), result.Stderr) {
		return hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED
	}
	return hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL
}

// unknownCommand reports whether a failed run is argparse rejecting the
// versioned command cli: its name as an invalid choice, or as a whole token of
// the unrecognized arguments. A longer word, an option or a path that merely
// contains the name is not the command.
func unknownCommand(cli string, exitCode, outputLength int, stderr string) bool {
	if !versionedCommands[cli] || exitCode != usageExitStatus || outputLength != 0 {
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
