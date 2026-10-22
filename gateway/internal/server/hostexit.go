package server

import (
	"errors"
	"os/exec"

	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

// usageExitStatus is argparse's exit status for an unknown command or option.
const usageExitStatus = 2

// exitFailureCategory types a host command that exited without a reply. A
// usage exit with nothing on stdout means the checkout does not know the
// command: it predates this gateway, or predates `vibesys host` entirely. The
// verdict is keyed on the exit status and the empty reply, never on stderr.
func exitFailureCategory(result hostcommand.Result, err error) hostv1.FailureCategory {
	var exit *exec.ExitError
	if result.ExitCode == usageExitStatus && len(result.Output) == 0 && (err == nil || errors.As(err, &exit)) {
		return hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED
	}
	return hostv1.FailureCategory_FAILURE_CATEGORY_INTERNAL
}
