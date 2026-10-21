package main_test

import (
	"bytes"
	"context"
	"encoding/binary"
	"errors"
	"hash/crc32"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/uw-syfi/vibesys/gateway/gen"
	apiv1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/api/v1"
	storev1 "github.com/uw-syfi/vibesys/gateway/gen/vibesys/store/v1"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
	"github.com/uw-syfi/vibesys/gateway/internal/runlog"
	"github.com/uw-syfi/vibesys/gateway/internal/server"
	"google.golang.org/grpc"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/protobuf/proto"
)

// The single CLI process integration verifies real stdio descriptors, including
// unblocking the stdin reader when the client closes its gRPC connection.
func TestConnectCLI(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	binary := filepath.Join(t.TempDir(), "vibesysd")
	build := exec.CommandContext(ctx, "go", "build", "-o", binary, ".")
	if output, err := build.CombinedOutput(); err != nil {
		t.Fatalf("build: %v\n%s", err, output)
	}
	t.Run("Tail", func(t *testing.T) { testTailCLI(t, binary) })
	t.Run("closed-output", func(t *testing.T) { testClosedOutput(t, binary) })
}

func testTailCLI(t *testing.T, binary string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root, err := os.MkdirTemp("/tmp", "g")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := lifecycle.Listen(root, gen.APISchemaHash)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	served := make(chan error, 1)
	go func() { served <- server.Serve(ctx, endpoint.Listener, server.Config{Root: root, Version: "cli-test"}) }()
	defer func() {
		cancel()
		select {
		case err := <-served:
			if err != nil {
				t.Error(err)
			}
		case <-time.After(10 * time.Second):
			t.Error("server did not stop")
		}
	}()
	run := t.TempDir()
	if err := os.Mkdir(filepath.Join(root, "runs"), 0700); err != nil {
		t.Fatal(err)
	}
	if err := os.Symlink(run, filepath.Join(root, "runs", "run-1")); err != nil {
		t.Fatal(err)
	}
	inputReader, inputWriter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer inputReader.Close()
	defer inputWriter.Close()
	outputReader, outputWriter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer outputReader.Close()
	defer outputWriter.Close()
	command := exec.CommandContext(ctx, binary, "connect", "--root", root)
	command.Stdin = inputReader
	command.Stdout = outputWriter
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	inputReader.Close()
	outputWriter.Close()
	waited := make(chan error, 1)
	go func() { waited <- command.Wait() }()
	defer func() {
		inputWriter.Close()
		outputReader.Close()
		select {
		case err := <-waited:
			if err != nil {
				t.Errorf("connect: %v, stderr: %s", err, stderr.String())
			}
		case <-time.After(10 * time.Second):
			command.Process.Kill()
			t.Error("connect did not stop")
		}
	}()
	duplex := &processConnection{input: inputWriter, output: outputReader}
	var dialed atomic.Bool
	connection, err := grpc.NewClient("passthrough:///stdio", grpc.WithTransportCredentials(insecure.NewCredentials()), grpc.WithContextDialer(func(context.Context, string) (net.Conn, error) {
		if dialed.Swap(true) {
			return nil, errors.New("test process connection already used")
		}
		return duplex, nil
	}))
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	c := apiv1.NewHostServiceClient(connection)
	if _, err := c.Hello(ctx, &apiv1.HelloRequest{ApiSchemaHash: gen.APISchemaHash}); err != nil {
		t.Fatal(err)
	}
	tail, err := c.Tail(ctx, &apiv1.TailRequest{RunId: "run-1", FromSeq: 1})
	if err != nil {
		t.Fatal(err)
	}
	if _, err := tail.Header(); err != nil {
		t.Fatal(err)
	}
	record, err := proto.Marshal(&storev1.Record{Seq: 1, Body: &storev1.Record_LegacyEvent{LegacyEvent: &storev1.LegacyEvent{Json: `{"event":"hello"}`}}})
	if err != nil {
		t.Fatal(err)
	}
	data := append([]byte(runlog.Magic), frame(record)...)
	if err := os.WriteFile(filepath.Join(run, "run.log"), data, 0600); err != nil {
		t.Fatal(err)
	}
	response, err := tail.Recv()
	if err != nil || response.GetThroughSeq() != 1 || len(response.GetRecords()) != 1 || !bytes.Equal(response.GetRecords()[0], record) {
		t.Fatalf("Tail over connect = %v, %v", response, err)
	}
}

func frame(payload []byte) []byte {
	frame := binary.AppendUvarint(nil, uint64(len(payload)))
	frame = binary.LittleEndian.AppendUint32(frame, crc32.Checksum(payload, crc32.MakeTable(crc32.Castagnoli)))
	return append(frame, payload...)
}

type processConnection struct {
	input, output *os.File
	once          sync.Once
}

func (c *processConnection) Read(p []byte) (int, error)  { return c.output.Read(p) }
func (c *processConnection) Write(p []byte) (int, error) { return c.input.Write(p) }
func (c *processConnection) Close() error {
	c.once.Do(func() { c.input.Close(); c.output.Close() })
	return nil
}
func (*processConnection) LocalAddr() net.Addr  { return &net.UnixAddr{Name: "client", Net: "unix"} }
func (*processConnection) RemoteAddr() net.Addr { return &net.UnixAddr{Name: "gateway", Net: "unix"} }
func (c *processConnection) SetDeadline(deadline time.Time) error {
	return errors.Join(c.SetReadDeadline(deadline), c.SetWriteDeadline(deadline))
}
func (c *processConnection) SetReadDeadline(deadline time.Time) error {
	return c.output.SetReadDeadline(deadline)
}
func (c *processConnection) SetWriteDeadline(deadline time.Time) error {
	return c.input.SetWriteDeadline(deadline)
}

func testClosedOutput(t *testing.T, binary string) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	root, err := os.MkdirTemp("/tmp", "g")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	endpoint, err := lifecycle.Listen(root, gen.APISchemaHash)
	if err != nil {
		t.Fatal(err)
	}
	defer endpoint.Close()
	endpoint.Listener.SetDeadline(time.Now().Add(10 * time.Second))
	inputReader, inputWriter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	defer inputReader.Close()
	defer inputWriter.Close()
	outputReader, outputWriter, err := os.Pipe()
	if err != nil {
		t.Fatal(err)
	}
	outputReader.Close()
	defer outputWriter.Close()
	command := exec.CommandContext(ctx, binary, "connect", "--root", root)
	command.Stdin = inputReader
	command.Stdout = outputWriter
	var stderr bytes.Buffer
	command.Stderr = &stderr
	if err := command.Start(); err != nil {
		t.Fatal(err)
	}
	inputReader.Close()
	outputWriter.Close()
	done := make(chan error, 1)
	go func() { done <- command.Wait() }()
	connection, err := endpoint.Listener.Accept()
	if err != nil {
		command.Process.Kill()
		t.Fatal(err)
	}
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(10 * time.Second))
	// stdin stays open, so only this downstream write can terminate Copy.
	// With default SIGPIPE handling, writing fd 1 kills connect here.
	if _, err := connection.Write([]byte("downstream")); err != nil {
		t.Fatal(err)
	}
	select {
	case err := <-done:
		if err != nil {
			t.Fatalf("closed output: %v, stderr: %s", err, stderr.String())
		}
	case <-ctx.Done():
		command.Process.Kill()
		t.Fatal(ctx.Err())
	}
}
