package wire

import (
	"context"
	"errors"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
)

var failureKinds = []struct {
	domain core.FailureKind
	wire   cliv1.FailureKind
	code   codes.Code
}{
	{core.FailureInvalidInput, cliv1.FailureKind_FAILURE_KIND_INVALID_INPUT, codes.InvalidArgument},
	{core.FailureNotFound, cliv1.FailureKind_FAILURE_KIND_NOT_FOUND, codes.NotFound},
	{core.FailureConflict, cliv1.FailureKind_FAILURE_KIND_CONFLICT, codes.FailedPrecondition},
	{core.FailureUnavailable, cliv1.FailureKind_FAILURE_KIND_UNAVAILABLE, codes.Unavailable},
	{core.FailureIncompatible, cliv1.FailureKind_FAILURE_KIND_INCOMPATIBLE, codes.FailedPrecondition},
	{core.FailureEnvironmentMismatch, cliv1.FailureKind_FAILURE_KIND_ENVIRONMENT_MISMATCH, codes.FailedPrecondition},
	{core.FailureNotMeasured, cliv1.FailureKind_FAILURE_KIND_NOT_MEASURED, codes.FailedPrecondition},
	{core.FailureInternal, cliv1.FailureKind_FAILURE_KIND_INTERNAL, codes.Internal},
}

// Failure returns the contract's message for a failure.
func Failure(f core.Failure) *cliv1.Failure {
	out := &cliv1.Failure{Kind: cliv1.FailureKind_FAILURE_KIND_INTERNAL, Message: f.Message}
	for _, p := range failureKinds {
		if p.domain == f.Kind {
			out.Kind = p.wire
		}
	}
	return out
}

// ToFailure returns the domain failure for the contract's message. A kind this build does not
// know is an internal failure with the message kept.
func ToFailure(f *cliv1.Failure) core.Failure {
	out := core.Failure{Kind: core.FailureInternal, Message: f.GetMessage()}
	for _, p := range failureKinds {
		if p.wire == f.GetKind() {
			out.Kind = p.domain
		}
	}
	return out
}

// Error returns the error a gRPC handler returns for err: a status whose details carry a Failure.
// A *core.Failure keeps its kind. A cancelled or timed-out context is what the caller asked for,
// so it keeps its own code. Anything else is an internal failure that names the cause.
func Error(err error) error {
	if err == nil {
		return nil
	}
	if _, ok := status.FromError(err); ok && status.Code(err) != codes.Unknown {
		return err
	}
	var f *core.Failure
	switch {
	case errors.As(err, &f):
	case errors.Is(err, context.Canceled):
		return status.Error(codes.Canceled, err.Error())
	case errors.Is(err, context.DeadlineExceeded):
		return status.Error(codes.DeadlineExceeded, err.Error())
	default:
		f = &core.Failure{Kind: core.FailureInternal, Message: err.Error()}
	}
	code := codes.Internal
	for _, p := range failureKinds {
		if p.domain == f.Kind {
			code = p.code
		}
	}
	st := status.New(code, f.Message)
	if withDetails, derr := st.WithDetails(Failure(*f)); derr == nil {
		st = withDetails
	}
	return st.Err()
}

// FailureOf returns the failure an error from a gRPC call carries. A status with a Failure in its
// details gives that; a cancelled call is the caller's own cancel and gives ok false; a status
// without details, such as a connection that failed, is unavailable.
func FailureOf(err error) (f core.Failure, ok bool) {
	if err == nil {
		return core.Failure{}, false
	}
	var typed *core.Failure
	if errors.As(err, &typed) {
		return *typed, true
	}
	st, isStatus := status.FromError(err)
	if !isStatus {
		return core.Failure{Kind: core.FailureInternal, Message: err.Error()}, true
	}
	for _, d := range st.Details() {
		if detail, isFailure := d.(*cliv1.Failure); isFailure {
			return ToFailure(detail), true
		}
	}
	switch st.Code() {
	case codes.Canceled, codes.DeadlineExceeded:
		return core.Failure{}, false
	case codes.Unavailable:
		return core.Failure{Kind: core.FailureUnavailable, Message: st.Message()}, true
	}
	return core.Failure{Kind: core.FailureInternal, Message: st.Message()}, true
}

// Unavailable returns the error for something that cannot be reached or done now.
func Unavailable(message string) error {
	return &core.Failure{Kind: core.FailureUnavailable, Message: message}
}

// Broken says the call ended because its connection failed or its daemon went away, so the error
// says nothing about what the call asked: no failure came back, only the end of the transport. A
// daemon that stops ends its streams with a cancel of its own, which is the same thing to a caller
// whose context is live.
func Broken(err error) bool {
	st, isStatus := status.FromError(err)
	if !isStatus || err == nil {
		return false
	}
	for _, d := range st.Details() {
		if _, isFailure := d.(*cliv1.Failure); isFailure {
			return false
		}
	}
	return st.Code() == codes.Unavailable || st.Code() == codes.Canceled
}

func hasKind(err error, kind core.FailureKind) bool {
	f, ok := FailureOf(err)
	return ok && f.Kind == kind
}

// IsNotFound says err is a failure of kind not found.
func IsNotFound(err error) bool { return hasKind(err, core.FailureNotFound) }

// IsInvalidInput says err is a failure of kind invalid input.
func IsInvalidInput(err error) bool { return hasKind(err, core.FailureInvalidInput) }

// IsIncompatible says err is a failure of kind incompatible.
func IsIncompatible(err error) bool { return hasKind(err, core.FailureIncompatible) }

// IsUnavailable says err is an unavailable failure: a typed one, or a status that carries one or
// stands for a connection that failed.
func IsUnavailable(err error) bool { return hasKind(err, core.FailureUnavailable) }
