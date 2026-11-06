package wire

import (
	"context"
	"errors"
	"slices"
	"strings"

	"buf.build/go/protovalidate"
	"google.golang.org/grpc"
	"google.golang.org/protobuf/proto"

	"github.com/uw-syfi/vibesys/cli/internal/core"
)

// Validate holds a message to the protovalidate rules its definition states, which are the one
// statement of what a receiver assumes. A message that breaks them is a *core.Failure of kind
// invalid input that names every field that does, in the order of their paths, so the answer to a
// bad request or a damaged record points at the field and not at a decoder deep inside.
func Validate(m proto.Message) error {
	err := protovalidate.Validate(m)
	if err == nil {
		return nil
	}
	var broken *protovalidate.ValidationError
	if !errors.As(err, &broken) {
		// The rules themselves did not compile or ran out of budget: not the sender's fault.
		return &core.Failure{Kind: core.FailureInternal, Message: "checking " + string(m.ProtoReflect().Descriptor().FullName()) + " against its rules: " + err.Error()}
	}
	found := make([]string, 0, len(broken.Violations))
	for _, v := range broken.Violations {
		path := protovalidate.FieldPathString(v.Proto.GetField())
		if path == "" {
			path = string(m.ProtoReflect().Descriptor().Name())
		}
		found = append(found, path+": "+v.Proto.GetMessage())
	}
	slices.Sort(found)
	return invalid("%s", strings.Join(found, "; "))
}

// RequestValidator is the unary server interceptor that validates each request before a handler
// sees it, so every RPC refuses a malformed request at its edge the same way.
func RequestValidator() grpc.UnaryServerInterceptor {
	return func(ctx context.Context, req any, _ *grpc.UnaryServerInfo, next grpc.UnaryHandler) (any, error) {
		if m, ok := req.(proto.Message); ok {
			if err := Validate(m); err != nil {
				return nil, Error(err)
			}
		}
		return next(ctx, req)
	}
}

// StreamRequestValidator is RequestValidator for a call that streams: the request a server
// stream receives, and each message a client stream sends.
func StreamRequestValidator() grpc.StreamServerInterceptor {
	return func(srv any, stream grpc.ServerStream, _ *grpc.StreamServerInfo, next grpc.StreamHandler) error {
		return next(srv, validatingStream{stream})
	}
}

type validatingStream struct{ grpc.ServerStream }

func (s validatingStream) RecvMsg(m any) error {
	if err := s.ServerStream.RecvMsg(m); err != nil {
		return err
	}
	if p, ok := m.(proto.Message); ok {
		return Error(Validate(p))
	}
	return nil
}
