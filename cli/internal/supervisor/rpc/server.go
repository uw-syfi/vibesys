package rpc

import (
	"context"
	"errors"

	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	cliv1 "github.com/uw-syfi/vibesys/cli/gen/vibesys/cli/v1"
	"github.com/uw-syfi/vibesys/cli/internal/core"
	"github.com/uw-syfi/vibesys/cli/internal/supervisor"
	"github.com/uw-syfi/vibesys/cli/internal/wire"
)

// Server serves one Supervisor as the SupervisorService.
type Server struct {
	cliv1.UnimplementedSupervisorServiceServer
	supervisor *supervisor.Supervisor
	binary     string
}

var _ cliv1.SupervisorServiceServer = (*Server)(nil)

// NewServer returns a server for the supervisor. binaryVersion is the build's version string,
// which the handshake reports.
func NewServer(s *supervisor.Supervisor, binaryVersion string) *Server {
	return &Server{supervisor: s, binary: binaryVersion}
}

// Handshake implements cliv1.SupervisorServiceServer.
func (s *Server) Handshake(_ context.Context, req *cliv1.HandshakeRequest) (*cliv1.HandshakeResponse, error) {
	if req.GetComponent() != cliv1.Component_COMPONENT_DAEMON {
		return nil, wire.Error(&core.Failure{Kind: core.FailureInvalidInput, Message: "a supervisor serves the daemon only"})
	}
	ours := core.CurrentVersions()
	compat := core.CheckCompatibility(wire.ToVersions(req.GetVersions()), ours)
	return &cliv1.HandshakeResponse{Versions: wire.BuildVersions(s.binary, ours), Compatibility: wire.Compatibility(compat)}, nil
}

// Start implements cliv1.SupervisorServiceServer.
func (s *Server) Start(context.Context, *cliv1.StartRequest) (*cliv1.StartResponse, error) {
	if err := s.supervisor.Start(); err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.StartResponse{}, nil
}

// Attach implements cliv1.SupervisorServiceServer.
func (s *Server) Attach(req *cliv1.AttachRequest, stream cliv1.SupervisorService_AttachServer) error {
	out, err := s.supervisor.Attach(stream.Context(), req.GetOffset(), func(offset uint64, data []byte) error {
		return stream.Send(&cliv1.AttachResponse{Item: &cliv1.AttachResponse_Output{Output: &cliv1.OutputChunk{Offset: offset, Data: data}}})
	})
	switch {
	case errors.Is(err, supervisor.ErrReleased):
		// To a caller a released supervisor is a gone one.
		return status.Error(codes.Unavailable, err.Error())
	case err != nil:
		return wire.Error(err)
	}
	return stream.Send(&cliv1.AttachResponse{Item: &cliv1.AttachResponse_Outcome{Outcome: wire.Outcome(out)}})
}

// CancelJob implements cliv1.SupervisorServiceServer.
func (s *Server) CancelJob(_ context.Context, req *cliv1.CancelJobRequest) (*cliv1.CancelJobResponse, error) {
	s.supervisor.Cancel(req.GetReason())
	return &cliv1.CancelJobResponse{}, nil
}

// Release implements cliv1.SupervisorServiceServer.
func (s *Server) Release(context.Context, *cliv1.ReleaseRequest) (*cliv1.ReleaseResponse, error) {
	if err := s.supervisor.Release(); err != nil {
		return nil, wire.Error(err)
	}
	return &cliv1.ReleaseResponse{}, nil
}
