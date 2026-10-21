package server_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/hostcommand"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

func TestSessionDiscoveryTailFenceAndBlobReads(t *testing.T) {
	f := start(t)
	writeFrames(t, f, 1)
	dir := filepath.Join(f.run, "sessions", "default")
	if err := os.MkdirAll(filepath.Join(dir, "blobs"), 0700); err != nil {
		t.Fatal(err)
	}
	record := &storev1.Record{Seq: 1, Epoch: 1, Body: &storev1.Record_Chat{Chat: &storev1.ChatRecord{Event: &storev1.ChatRecord_Token{Token: &storev1.ChatToken{RequestId: "q-1", Text: "hello"}}}}}
	first := encode(t, record)
	record.Seq = 2
	second := encode(t, record)
	write(t, filepath.Join(dir, "session.log"), false, append(append([]byte(runlog.Magic), frame(first)...), frame(second)...))
	write(t, filepath.Join(dir, "run.head"), false, encode(t, head(proto.Uint64(1))))
	client := client(t, f, false)
	skeleton, err := client.Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if err != nil || len(skeleton.GetSessions()) != 1 || skeleton.Sessions[0] != "default" {
		t.Fatalf("sessions: %v %v", skeleton, err)
	}
	blob := []byte("session evidence")
	digest := fmt.Sprintf("%x", sha256.Sum256(blob))
	write(t, filepath.Join(dir, "blobs", digest), false, blob)
	got, err := client.GetBlobs(f.ctx, &apiv1.GetBlobsRequest{RunId: "run-1", SessionId: "default", Sha256: []string{digest}})
	if err != nil || len(got.GetBlobs()) != 1 || !bytes.Equal(got.Blobs[0].Data, blob) {
		t.Fatalf("session blob: %v %v", got, err)
	}
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	tail, err := client.Tail(ctx, &apiv1.TailRequest{RunId: "run-1", SessionId: "default", FromSeq: 1})
	if err != nil {
		t.Fatal(err)
	}
	batch, err := tail.Recv()
	if err != nil || len(batch.GetRecords()) != 1 || !bytes.Equal(batch.Records[0], first) {
		t.Fatalf("session tail: %v %v", batch, err)
	}
	// Publishing the session's fence releases exactly its suffix.
	write(t, filepath.Join(dir, "head.tmp"), false, encode(t, head(proto.Uint64(2))))
	if err := os.Rename(filepath.Join(dir, "head.tmp"), filepath.Join(dir, "run.head")); err != nil {
		t.Fatal(err)
	}
	batch, err = tail.Recv()
	if err != nil || len(batch.GetRecords()) != 1 || !bytes.Equal(batch.Records[0], second) {
		t.Fatalf("session suffix: %v %v", batch, err)
	}
	invalid, err := client.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1", SessionId: "../escape"})
	if err == nil {
		_, err = invalid.Recv()
	}
	if status.Code(err) != codes.InvalidArgument {
		t.Fatalf("traversal: %v", err)
	}
}

func TestChatMessageHostCallRoundTrip(t *testing.T) {
	request := &apiv1.HostCallRequest{Checkout: "/src/vibesys", Method: &apiv1.HostCallRequest_ChatMessage{ChatMessage: &apiv1.ChatMessageRequest{ProjectRoot: "/project", RunId: "run-1", SessionId: "default", RequestId: "q-1", Text: "Why?"}}}
	runner := hostcommand.FakeRunner{Program: func(_ context.Context, c hostcommand.Command) (hostcommand.Result, error) {
		if len(c.Args) != 2 || c.Args[1] != "chat-message" {
			t.Errorf("method: %v", c.Args)
		}
		decoded := &apiv1.HostCallRequest{}
		if err := proto.Unmarshal(c.Input, decoded); err != nil || !proto.Equal(request, decoded) {
			t.Errorf("input: %v %v", decoded, err)
		}
		return hostcommand.Result{Output: encode(t, &apiv1.HostCallResponse{Result: &apiv1.HostCallResponse_ChatMessage{ChatMessage: &apiv1.ChatMessageResponse{SessionId: "default", RequestId: "q-1"}}})}, nil
	}}
	f := startWithConfig(t, server.Config{HostRunner: runner})
	response, err := client(t, f, false).HostCall(f.ctx, request)
	if err != nil || response.GetChatMessage().GetRequestId() != "q-1" {
		t.Fatalf("response: %v %v", response, err)
	}
}

func TestSessionListingRespectsSkeletonBudget(t *testing.T) {
	f := start(t)
	writeFrames(t, f, 1)
	for i := 0; i < 8100; i++ {
		id := fmt.Sprintf("%04d%s", i, strings.Repeat("a", 124))
		if err := os.MkdirAll(filepath.Join(f.run, "sessions", id), 0700); err != nil {
			t.Fatal(err)
		}
	}
	_, err := client(t, f, false).Skeleton(f.ctx, &apiv1.SkeletonRequest{RunId: "run-1"})
	if status.Code(err) != codes.ResourceExhausted {
		t.Fatalf("listing budget: %v", err)
	}
}
