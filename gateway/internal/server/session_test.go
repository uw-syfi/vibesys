package server_test

import (
	"bytes"
	"context"
	"crypto/sha256"
	"fmt"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/proto"
	"google.golang.org/protobuf/types/known/timestamppb"
	"math/rand"
	"os"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
	"testing/quick"
	"time"
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

func TestGeneratedChatListingUsesOnlyCommittedSessionPrefixes(t *testing.T) {
	f := start(t)
	writeFrames(t, f, 1)
	peer := client(t, f, false)
	property := func(commits [8]uint8) bool {
		root := filepath.Join(f.run, "sessions")
		if err := os.RemoveAll(root); err != nil {
			t.Fatal(err)
		}
		want := map[string]string{}
		for index, value := range commits {
			id := fmt.Sprintf("thread-%d", index)
			dir := filepath.Join(root, id)
			if err := os.MkdirAll(dir, 0700); err != nil {
				t.Fatal(err)
			}
			created := &storev1.Record{Seq: 1, Epoch: 1, At: timestamppb.New(time.Unix(int64(index), 0)), Body: &storev1.Record_Chat{Chat: &storev1.ChatRecord{Event: &storev1.ChatRecord_Created{Created: &storev1.ChatSessionCreated{Id: id, Provider: "claude", Model: "opus"}}}}}
			text := strings.Repeat("界", int(value)+1)
			question := &storev1.Record{Seq: 2, Epoch: 1, Body: &storev1.Record_Chat{Chat: &storev1.ChatRecord{Event: &storev1.ChatRecord_Message{Message: &storev1.ChatMessage{RequestId: "q-1", Role: storev1.ChatRole_CHAT_ROLE_USER, Content: &storev1.ChatMessage_Text{Text: text}}}}}}
			write(t, filepath.Join(dir, "session.log"), false, append(append([]byte(runlog.Magic), frame(encode(t, created))...), frame(encode(t, question))...))
			committed := uint64(value % 3)
			write(t, filepath.Join(dir, "run.head"), false, encode(t, head(proto.Uint64(committed))))
			if committed > 0 {
				want[id] = ""
			}
			if committed == 2 {
				title := []rune(text)
				if len(title) > 120 {
					title = title[:120]
				}
				want[id] = string(title)
			}
		}
		response, err := peer.ListChatSessions(f.ctx, &apiv1.ListChatSessionsRequest{RunId: "run-1"})
		if err != nil {
			t.Log(err)
			return false
		}
		got := map[string]string{}
		for _, session := range response.Sessions {
			got[session.Id] = session.Title
		}
		return reflect.DeepEqual(got, want)
	}
	if err := quick.Check(property, &quick.Config{Rand: rand.New(rand.NewSource(0))}); err != nil {
		t.Fatal(err)
	}
}
