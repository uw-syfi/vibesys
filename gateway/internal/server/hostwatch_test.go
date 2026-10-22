package server

import (
	"context"
	"testing"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	hostv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/host/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"google.golang.org/grpc"
	"google.golang.org/grpc/metadata"
	"google.golang.org/protobuf/proto"
)

type watchStream struct {
	grpc.ServerStream
	ctx  context.Context
	sent []*apiv1.WatchResponse
}

func (s *watchStream) Context() context.Context { return s.ctx }

func (s *watchStream) Send(response *apiv1.WatchResponse) error {
	s.sent = append(s.sent, response)
	return nil
}

func (s *watchStream) SendHeader(metadata.MD) error { return nil }
func (s *watchStream) SetHeader(metadata.MD) error  { return nil }
func (s *watchStream) SetTrailer(metadata.MD)       {}

func TestWatchForwardsTheLoginResponse(t *testing.T) {
	event := &hostv1.LoginResponse{
		RequestId: "request-a",
		Sequence:  1,
		Body:      &hostv1.LoginResponse_Started{Started: &hostv1.LoginStartedBody{Instruction: proto.String("Open https://a.example/x")}},
	}
	payload, err := proto.Marshal(&apiv1.WatchResponse{Result: &apiv1.WatchResponse_Login{Login: event}})
	if err != nil {
		t.Fatal(err)
	}
	service := &host{config: Config{Root: t.TempDir(), HostRunner: hostcommand.FakeRunner{Program: func(context.Context, hostcommand.Command) (hostcommand.Result, error) {
		return hostcommand.Result{Output: payload}, nil
	}}}}
	stream := &watchStream{ctx: context.Background()}
	request := &apiv1.WatchRequest{
		Checkout: "/checkout",
		Method:   &apiv1.WatchRequest_Login{Login: &hostv1.LoginRequest{RequestId: "request-a", Provider: "claude"}},
	}
	if err := service.Watch(request, stream); err != nil {
		t.Fatal(err)
	}
	if len(stream.sent) != 1 || stream.sent[0].GetLogin().GetRequestId() != "request-a" {
		t.Fatalf("sent %+v", stream.sent)
	}
	if stream.sent[0].GetLogin().GetStarted().GetInstruction() != "Open https://a.example/x" {
		t.Fatalf("instruction %q", stream.sent[0].GetLogin().GetStarted().GetInstruction())
	}
}
