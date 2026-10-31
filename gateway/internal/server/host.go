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

	"github.com/uw-syfi/vibesys/gateway/gen"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/machinestats"
	"github.com/uw-syfi/vibesys/gateway/internal/runindex"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/internal/subscription"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/keepalive"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

const batchBytes = 1 << 20

// Config is process wiring; the API hash comes only from generated code.
type Config struct {
	Root    string
	Version string
	Clock   Clock
	// Probes times the run index's liveness probes and their lease. A test that drives
	// them by hand sets it, apart from the Clock that paces the hub. Nil means Clock.
	Probes     Clock
	HostRunner hostcommand.Runner
	// Machine is what machine_stats samples.
	Machine machinestats.Host
}

// withDefaults fills what the caller left to the process: real time, real commands, this machine.
func (c Config) withDefaults() Config {
	if c.HostRunner == nil {
		c.HostRunner = hostcommand.ExecRunner{}
	}
	if c.Clock == nil {
		c.Clock = SystemClock{}
	}
	if c.Probes == nil {
		c.Probes = c.Clock
	}
	if c.Machine == nil {
		c.Machine = machinestats.SystemHost{}
	}
	return c
}

type host struct {
	apiv1.UnimplementedHostServiceServer
	config Config
	index  runlog.Index
	heads  headObserver
	topics *subscription.Hub
}

func (h *host) Hello(_ context.Context, request *apiv1.HelloRequest) (*apiv1.HelloResponse, error) {
	if request.GetApiSchemaHash() != schemahash.APISchemaHash {
		return nil, status.Errorf(codes.FailedPrecondition, "API schema hash mismatch: client %q, gateway %q", request.GetApiSchemaHash(), schemahash.APISchemaHash)
	}
	return &apiv1.HelloResponse{ApiSchemaHash: schemahash.APISchemaHash, GatewayVersion: h.config.Version}, nil
}

func (h *host) Tail(request *apiv1.TailRequest, stream apiv1.HostService_TailServer) error {
	path, err := h.logPath(request.GetRunId(), request.GetSessionId())
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
	var projection *runlog.Projection
	upgrade := runlog.NewInputUpgrade(nil)
	if request.GetSessionId() == "" {
		projection, err = projectionAt(filepath.Dir(path), position)
		if err == nil {
			upgrade = runlog.NewInputUpgrade(projection.State)
		}
	}
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
			payload, err = upgrade.Record(payload)
			if err != nil {
				readErr = err
				break
			}
			if projection != nil {
				if err := projection.Record(payload); err != nil {
					readErr = err
					break
				}
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
			if projection != nil {
				response.Head, err = runlog.ProjectHead(projection.State)
				if err != nil {
					return rpcError(err)
				}
			}
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
	config = config.withDefaults()
	server := grpc.NewServer(
		grpc.MaxSendMsgSize(gen.HostAPIMaxMessageBytes),
		grpc.UnaryInterceptor(validateUnary),
		grpc.StreamInterceptor(validateStream),
		grpc.KeepaliveParams(keepalive.ServerParameters{Time: KeepaliveTime, Timeout: KeepaliveTimeout}),
		grpc.StatsHandler(&connections{}),
	)
	service := &host{config: config}
	service.topics = newTopics(service)
	apiv1.RegisterHostServiceServer(server, service)
	stop := context.AfterFunc(ctx, server.Stop)
	defer stop()
	defer server.Stop()
	err := server.Serve(listener)
	if errors.Is(err, net.ErrClosed) || errors.Is(err, grpc.ErrServerStopped) {
		return nil
	}
	return err
}
