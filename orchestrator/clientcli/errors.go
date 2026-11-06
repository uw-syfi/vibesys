package clientcli

import (
	"context"
	"errors"
	"fmt"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/uw-syfi/vibesys/orchestrator/gen/vibesys/orchestrator/v1"
	"github.com/uw-syfi/vibesys/orchestrator/internal/client"
)

// The exit codes of every verb.
const (
	ExitOK          = 0
	ExitFailed      = 1 // the run failed, or the service refused
	ExitUsage       = 2
	ExitTimeout     = 3
	ExitUnavailable = 4 // no service, or one that cannot serve this client
)

// failure is an error that carries the exit code it ends the process with.
type failure struct {
	code  int
	usage bool
	msg   string
}

func (f *failure) Error() string { return f.msg }

func failed(format string, args ...any) error {
	return &failure{code: ExitFailed, msg: fmt.Sprintf(format, args...)}
}

func usageError(format string, args ...any) error {
	return &failure{code: ExitUsage, usage: true, msg: fmt.Sprintf(format, args...)}
}

func timedOut(format string, args ...any) error {
	return &failure{code: ExitTimeout, msg: fmt.Sprintf(format, args...)}
}

func unavailable(format string, args ...any) error {
	return &failure{code: ExitUnavailable, msg: fmt.Sprintf(format, args...)}
}

// exitCodeOf is the exit code an error ends the process with.
func exitCodeOf(err error) int {
	var f *failure
	if errors.As(err, &f) {
		return f.code
	}
	return ExitFailed
}

// classify turns an error from the service into a failure with the right exit code. A service
// that is down, or does not serve the orchestrator, is unavailable; the service's own reason
// is passed through.
func classify(err error) error {
	var f *failure
	var incompatible *client.IncompatibleError
	switch code := status.Code(err); {
	case err == nil, errors.As(err, &f):
		return err
	case errors.Is(err, context.Canceled), code == codes.Canceled:
		return failed("interrupted")
	case errors.As(err, &incompatible):
		if incompatible.Compatibility == pb.Compatibility_COMPATIBILITY_CLIENT_TOO_OLD {
			return failed("%v", err)
		}
		return unavailable("%v", err)
	case code == codes.Unavailable, code == codes.Unimplemented:
		return unavailable("the orchestrator is unavailable: %s", status.Convert(err).Message())
	case code != codes.Unknown:
		return failed("%s", status.Convert(err).Message())
	}
	return failed("%v", err)
}
