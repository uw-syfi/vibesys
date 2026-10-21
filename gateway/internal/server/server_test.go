package server_test

import (
	"bytes"
	"context"
	"encoding/binary"
	"github.com/uw-syfi/vibesys/gateway/gen"
	"hash/crc32"
	"net"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/internal/pipe"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"github.com/uw-syfi/vibesys/gateway/schemahash"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/status"
	"google.golang.org/protobuf/encoding/protowire"
	"google.golang.org/protobuf/proto"
)

type fixture struct {
	root   string
	run    string
	socket string
	ctx    context.Context
}

func start(t *testing.T) fixture {
	t.Helper()
	return startWithClock(t, nil)
}

func startWithClock(t *testing.T, clock server.Clock) fixture {
	return startWithConfig(t, server.Config{Clock: clock})
}

func startWithConfig(t *testing.T, config server.Config) fixture {
	t.Helper()
	// Keep Unix socket paths short even on hosts with long temporary roots.
	root, err := os.MkdirTemp("/tmp", "g")
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { os.RemoveAll(root) })
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	endpoint, err := lifecycle.Listen(root, schemahash.APISchemaHash)
	if err != nil {
		cancel()
		t.Fatal(err)
	}
	done := make(chan error, 1)
	go func() {
		config.Root = root
		config.Version = "test-version"
		done <- server.Serve(ctx, endpoint.Listener, config)
	}()
	t.Cleanup(func() {
		cancel()
		endpoint.Close()
		select {
		case err := <-done:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(10 * time.Second):
			t.Error("server did not stop")
		}
	})
	run := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "runs"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(run, filepath.Join(root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	return fixture{root: root, run: run, socket: lifecycle.SocketPath(root, schemahash.APISchemaHash), ctx: ctx}
}

func client(t *testing.T, f fixture, throughPipe bool) apiv1.HostServiceClient {
	t.Helper()
	dial := func(ctx context.Context, _ string) (net.Conn, error) {
		connection, err := (&net.Dialer{}).DialContext(ctx, "unix", f.socket)
		if err != nil {
			return nil, err
		}
		if !throughPipe {
			return connection, nil
		}
		front, back := net.Pipe()
		done := make(chan error, 1)
		go func() { done <- pipe.Copy(f.ctx, back, connection) }()
		t.Cleanup(func() {
			front.Close()
			select {
			case <-done:
			case <-time.After(10 * time.Second):
				t.Error("connect pipe did not stop")
			}
		})
		return front, nil
	}
	connection, err := grpc.NewClient("passthrough:///gateway", grpc.WithContextDialer(dial), grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithDefaultCallOptions(grpc.MaxCallRecvMsgSize(runlog.MaxRecordBytes+32)))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { connection.Close() })
	return apiv1.NewHostServiceClient(connection)
}

func payload(t *testing.T, seq uint64, text string) []byte {
	t.Helper()
	encoded, err := proto.Marshal(&storev1.Record{Seq: seq, Body: &storev1.Record_LegacyEvent{LegacyEvent: &storev1.LegacyEvent{Json: text}}})
	if err != nil {
		t.Fatal(err)
	}
	// Unknown fields must survive exactly; the gateway understands only seq.
	encoded = protowire.AppendTag(encoded, 100, protowire.BytesType)
	return protowire.AppendBytes(encoded, []byte{0, 255, 10})
}

func frame(payload []byte) []byte {
	data := binary.AppendUvarint(nil, uint64(len(payload)))
	data = binary.LittleEndian.AppendUint32(data, crc32.Checksum(payload, crc32.MakeTable(crc32.Castagnoli)))
	return append(data, payload...)
}

func write(t *testing.T, path string, appendTo bool, data []byte) {
	t.Helper()
	flags := os.O_CREATE | os.O_WRONLY
	if appendTo {
		flags |= os.O_APPEND
	} else {
		flags |= os.O_TRUNC
	}
	file, err := os.OpenFile(path, flags, 0600)
	if err != nil {
		t.Fatal(err)
	}
	defer file.Close()
	if _, err := file.Write(data); err != nil {
		t.Fatal(err)
	}
}

func receive(t *testing.T, stream apiv1.HostService_TailClient, want []byte, seq uint64) {
	t.Helper()
	got, err := stream.Recv()
	if err != nil {
		t.Fatal(err)
	}
	if len(got.Records) != 1 || !bytes.Equal(got.Records[0], want) || got.ThroughSeq != seq {
		t.Fatalf("Tail = %v, want seq %d and exact record bytes", got, seq)
	}
}

func TestHello(t *testing.T) {
	f := start(t)
	c := client(t, f, false)
	hello, err := c.Hello(f.ctx, &apiv1.HelloRequest{ApiSchemaHash: schemahash.APISchemaHash})
	if err != nil || hello.GetApiSchemaHash() != schemahash.APISchemaHash || hello.GetGatewayVersion() != "test-version" {
		t.Fatalf("Hello = %v, %v", hello, err)
	}
	for _, hash := range []string{"", "other-hash", schemahash.APISchemaHash[:lifecycle.HashPrefixLength] + strings.Repeat("0", 64-lifecycle.HashPrefixLength)} {
		_, err := c.Hello(f.ctx, &apiv1.HelloRequest{ApiSchemaHash: hash})
		if status.Code(err) != codes.FailedPrecondition || !strings.Contains(err.Error(), schemahash.APISchemaHash) || !strings.Contains(err.Error(), `"`+hash+`"`) {
			t.Fatalf("mismatch = %v", err)
		}
	}
}

func TestTailExistingThenLive(t *testing.T) {
	for _, throughPipe := range []bool{false, true} {
		t.Run(map[bool]string{false: "unix", true: "connect-pipe"}[throughPipe], func(t *testing.T) {
			f := start(t)
			one, two, three := payload(t, 1, "one"), payload(t, 2, "two"), payload(t, 3, "three")
			path := filepath.Join(f.run, "run.log")
			data := append(append([]byte(runlog.Magic), frame(one)...), frame(two)...)
			write(t, path, false, data)
			stream, err := client(t, f, throughPipe).Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1", FromSeq: 2})
			if err != nil {
				t.Fatal(err)
			}
			receive(t, stream, two, 2)
			write(t, path, true, frame(three))
			receive(t, stream, three, 3)
		})
	}
}

func TestTailBeforeFileCreation(t *testing.T) {
	f := start(t)
	stream, err := client(t, f, false).Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Header(); err != nil {
		t.Fatal(err)
	}
	record := payload(t, 1, "created")
	write(t, filepath.Join(f.run, "run.log"), false, append([]byte(runlog.Magic), frame(record)...))
	receive(t, stream, record, 1)
}

func TestTailErrors(t *testing.T) {
	cases := []struct {
		name, id string
		data     []byte
		code     codes.Code
	}{
		{name: "invalid-id", id: "../escape", code: codes.InvalidArgument},
		{name: "unknown-run", id: "unknown", code: codes.NotFound},
		{name: "bad-magic", id: "run-1", data: []byte("VSRLOG02"), code: codes.DataLoss},
		{name: "bad-envelope", id: "run-1", data: append([]byte(runlog.Magic), frame([]byte{0x0a, 0})...), code: codes.DataLoss},
	}
	for _, tc := range cases {
		t.Run(tc.name, func(t *testing.T) {
			f := start(t)
			if tc.data != nil {
				write(t, filepath.Join(f.run, "run.log"), false, tc.data)
			}
			stream, err := client(t, f, false).Tail(f.ctx, &apiv1.TailRequest{RunId: tc.id})
			if err == nil {
				_, err = stream.Recv()
			}
			if status.Code(err) != tc.code {
				t.Fatalf("Tail = %v, want %v", err, tc.code)
			}
		})
	}
}

func TestTailDeliversPrefixBeforeCorruption(t *testing.T) {
	for _, badCRC := range []bool{false, true} {
		t.Run(map[bool]string{false: "duplicate-sequence", true: "checksum"}[badCRC], func(t *testing.T) {
			f := start(t)
			first := payload(t, 1, "first")
			second := frame(payload(t, 1, "duplicate"))
			if badCRC {
				second = frame(payload(t, 2, "damaged"))
				second[len(second)-1] ^= 1
			}
			data := append(append([]byte(runlog.Magic), frame(first)...), second...)
			write(t, filepath.Join(f.run, "run.log"), false, data)
			stream, err := client(t, f, false).Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
			if err != nil {
				t.Fatal(err)
			}
			receive(t, stream, first, 1)
			if _, err := stream.Recv(); status.Code(err) != codes.DataLoss {
				t.Fatalf("Tail = %v, want DATA_LOSS", err)
			}
		})
	}
}

func TestTailBatchBounds(t *testing.T) {
	f := start(t)
	records := make([][]byte, 100)
	data := []byte(runlog.Magic)
	for i := range records {
		records[i] = payload(t, uint64(i+1), strings.Repeat("x", 16<<10))
		data = append(data, frame(records[i])...)
	}
	write(t, filepath.Join(f.run, "run.log"), false, data)
	stream, err := client(t, f, false).Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	count := 0
	for count < len(records) {
		response, err := stream.Recv()
		if err != nil {
			t.Fatal(err)
		}
		if proto.Size(response) > 1<<20 || len(response.Records) == 0 {
			t.Fatalf("batch size = %d", proto.Size(response))
		}
		for _, got := range response.Records {
			if count >= len(records) || !bytes.Equal(got, records[count]) {
				t.Fatalf("unexpected record %d", count)
			}
			count++
		}
		if response.ThroughSeq != uint64(count) {
			t.Fatalf("through_seq = %d, want %d", response.ThroughSeq, count)
		}
	}
}

func TestSlowTailDoesNotBlockOtherStreams(t *testing.T) {
	f := start(t)
	c := client(t, f, false)
	data := []byte(runlog.Magic)
	// Account for the protobuf envelope so each record reaches the wire cap.
	text := strings.Repeat("x", gen.RunLogMaxRecordBytes)
	text = text[:len(text)-(len(payload(t, 1, text))-gen.RunLogMaxRecordBytes)]
	first := payload(t, 1, text)
	if len(first) != gen.RunLogMaxRecordBytes {
		t.Fatalf("record size = %d, want %d", len(first), gen.RunLogMaxRecordBytes)
	}
	for seq := uint64(1); seq <= 8; seq++ {
		data = append(data, frame(payload(t, seq, text))...)
	}
	write(t, filepath.Join(f.run, "run.log"), false, data)
	slow, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := slow.Header(); err != nil {
		t.Fatal(err)
	}
	// Leave this stream unread. An independent RPC and a second Tail on the same
	// HTTP/2 connection must still progress, regardless of the slow stream's window.
	if _, err := c.Hello(f.ctx, &apiv1.HelloRequest{ApiSchemaHash: schemahash.APISchemaHash}); err != nil {
		t.Fatal(err)
	}
	fast, err := c.Tail(f.ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	receive(t, fast, first, 1)
}

func TestTailCancellationKeepsConnectionAlive(t *testing.T) {
	f := start(t)
	c := client(t, f, false)
	ctx, cancel := context.WithCancel(f.ctx)
	defer cancel()
	stream, err := c.Tail(ctx, &apiv1.TailRequest{RunId: "run-1"})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := stream.Header(); err != nil {
		t.Fatal(err)
	}
	cancel()
	if _, err := stream.Recv(); status.Code(err) != codes.Canceled {
		t.Fatalf("cancel = %v", err)
	}
	if _, err := c.Hello(f.ctx, &apiv1.HelloRequest{ApiSchemaHash: schemahash.APISchemaHash}); err != nil {
		t.Fatal(err)
	}
}
