package server

import (
	"context"

	"buf.build/go/protovalidate"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
)

// The schema owns field rules. Interceptors validate both directions, including
// every streamed message, before a handler or peer can observe it.
func validateMessage(value any, code codes.Code) error {
	message, ok := value.(proto.Message)
	if !ok {
		return status.Error(codes.Internal, "gateway message is not protobuf")
	}
	if err := protovalidate.Validate(message); err != nil {
		return status.Error(code, err.Error())
	}
	return nil
}

func validateUnary(ctx context.Context, request any, _ *grpc.UnaryServerInfo, handler grpc.UnaryHandler) (any, error) {
	// HostCall input validation belongs to its Python command so admission
	// failures retain the typed configuration_failed result contract.
	_, hostCall := request.(*apiv1.HostCallRequest)
	if err := validateMessage(request, codes.InvalidArgument); err != nil && !hostCall {
		return nil, err
	}
	response, err := handler(ctx, request)
	if err != nil {
		return nil, err
	}
	if err := validateMessage(response, codes.Internal); err != nil {
		// HostCall promises typed diagnostics for invalid subprocess replies too.
		if hostCall {
			reply, _ := response.(*apiv1.HostCallResponse)
			failure := reply.GetConfigurationFailed()
			return hostFailure("invalid_host_response", err.Error(), int(failure.GetExitCode()), failure.GetStderr()), nil
		}
		return nil, err
	}
	return response, nil
}

type validatedStream struct{ grpc.ServerStream }

func (s validatedStream) RecvMsg(message any) error {
	if err := s.ServerStream.RecvMsg(message); err != nil {
		return err
	}
	return validateMessage(message, codes.InvalidArgument)
}

func (s validatedStream) SendMsg(message any) error {
	if err := validateMessage(message, codes.Internal); err != nil {
		return err
	}
	return s.ServerStream.SendMsg(message)
}

func validateStream(server any, stream grpc.ServerStream, _ *grpc.StreamServerInfo, handler grpc.StreamHandler) error {
	return handler(server, validatedStream{stream})
}
