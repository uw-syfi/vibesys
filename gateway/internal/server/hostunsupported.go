package server

import (
	"errors"
	"fmt"

	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
)

// unsupportedMessage is the whole diagnostic for a host command its checkout
// does not know. The checkout's usage dump is never forwarded: a client shows
// the category, and the text names no argparse output, path or option.
func unsupportedMessage(cli string) string {
	return fmt.Sprintf("this host's VibeSys does not provide %q; update it", cli)
}

// commandFailure types a host command that exited without a reply and words
// it. Only the unsupported category drops the command's stderr.
func commandFailure(cli string, result hostcommand.Result, err error) (hostv1.FailureCategory, string) {
	category := exitFailureCategory(cli, result, err)
	if category == hostv1.FailureCategory_FAILURE_CATEGORY_UNSUPPORTED {
		return category, unsupportedMessage(cli)
	}
	message := fmt.Sprintf("vibesys host %s exited %d", cli, result.ExitCode)
	if err != nil {
		message += ": " + err.Error()
	}
	return category, message + "\n" + result.Stderr
}

// unsupportedStream reports whether a stream that sent nothing ended with
// argparse rejecting the versioned command cli.
func unsupportedStream(cli string, err error) bool {
	var exit *hostcommand.ExitError
	return errors.As(err, &exit) && unknownCommand(cli, exit.Code, 0, exit.Stderr)
}
