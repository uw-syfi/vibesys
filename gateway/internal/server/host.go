// Package server implements the Host API using only the published run index
// and append-only log. It has no product policy or Python dependencies.
package server

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"path/filepath"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runindex"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const batchBytes = 1 << 20

// Config is process wiring; the API hash comes only from generated code.
type Config struct {
	Root    string
	Version string
	Clock   Clock
}

type host struct {
	apiv1.UnimplementedHostServiceServer
	config Config
	index  runlog.Index
}

func (h *host) Hello(_ context.Context, request *apiv1.HelloRequest) (*apiv1.HelloResponse, error) {
	if request.GetApiSchemaHash() != schemahash.APISchemaHash {
		return nil, status.Errorf(codes.FailedPrecondition, "API schema hash mismatch: client %q, gateway %q", request.GetApiSchemaHash(), schemahash.APISchemaHash)
	}
	return &apiv1.HelloResponse{ApiSchemaHash: schemahash.APISchemaHash, GatewayVersion: h.config.Version}, nil
}

func (h *host) Tail(request *apiv1.TailRequest, stream apiv1.HostService_TailServer) error {
	path, err := runindex.Resolve(h.config.Root, request.GetRunId())
	if err != nil {
		return rpcError(err)
	}
	follower, err := runlog.Open(path)
	if err != nil {
		return rpcError(err)
	}
	defer follower.Close()
	cap, _, err := committed(filepath.Dir(path))
	if err != nil {
		return rpcError(err)
	}
	position, err := h.seek(path, request.GetFromSeq(), cap)
	if err != nil {
		return rpcError(err)
	}
	follower.Seek(position, &h.index, func() (uint64, bool, error) { return committed(filepath.Dir(path)) })
	// Headers publish that discovery and watch installation succeeded. A caller
	// can now append without depending on goroutine scheduling or polling.
	if err := stream.SendHeader(metadata.MD{}); err != nil {
		return err
	}
	previous := uint64(0)
	seen := false
	for {
		payloads, readErr := follower.Read(stream.Context(), batchBytes-16)
		response := &apiv1.TailResponse{}
		for _, payload := range payloads {
			seq, err := runlog.Sequence(payload)
			if err != nil {
				readErr = err
				break
			}
			if seen && seq <= previous {
				readErr = status.Errorf(codes.DataLoss, "run.log sequence %d does not follow %d", seq, previous)
				break
			}
			seen = true
			previous = seq
			if seq < request.GetFromSeq() {
				continue
			}
			response.Records = append(response.Records, payload)
			response.ThroughSeq = seq
		}
		if len(response.Records) > 0 {
			if err := stream.Send(response); err != nil {
				return err
			}
		}
		if readErr != nil {
			return rpcError(readErr)
		}
	}
}

func rpcError(err error) error {
	if _, ok := status.FromError(err); ok {
		return err
	}
	var corrupt *runlog.Corruption
	switch {
	case errors.Is(err, context.Canceled), errors.Is(err, context.DeadlineExceeded):
		return status.FromContextError(err).Err()
	case errors.Is(err, runindex.ErrInvalidID):
		return status.Error(codes.InvalidArgument, err.Error())
	case errors.Is(err, os.ErrNotExist):
		return status.Error(codes.NotFound, err.Error())
	case errors.Is(err, os.ErrPermission):
		return status.Error(codes.PermissionDenied, err.Error())
	case errors.As(err, &corrupt):
		return status.Error(codes.DataLoss, err.Error())
	default:
		return status.Error(codes.Internal, fmt.Sprintf("gateway: %v", err))
	}
}

// Serve owns the gRPC server until listener closure or context cancellation.
// The caller owns socket publication and connection/idle accounting.
func Serve(ctx context.Context, listener net.Listener, config Config) error {
	if config.Clock == nil {
		config.Clock = SystemClock{}
	}
	server := grpc.NewServer(grpc.MaxSendMsgSize(maxMessageBytes))
	apiv1.RegisterHostServiceServer(server, &host{config: config})
	stop := context.AfterFunc(ctx, server.Stop)
	defer stop()
	defer server.Stop()
	err := server.Serve(listener)
	if errors.Is(err, net.ErrClosed) || errors.Is(err, grpc.ErrServerStopped) {
		return nil
	}
	return err
}
