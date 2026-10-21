package connect

import (
	"context"
	"fmt"
	"io"
	"net"
	"os"
	"path/filepath"
	"syscall"
	"testing"
	"time"
)

func TestDetachedStartup(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "gw-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	socket := filepath.Join(root, "gateway.sock")
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	binary, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	connection, err := Dial(ctx, socket, binary, []string{"-test.run=^TestDetachedHelper$", "--", socket})
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(10 * time.Second))
	var pid, group, session int
	if _, err := fmt.Fscanf(connection, "%d %d %d\n", &pid, &group, &session); err != nil {
		t.Fatal(err)
	}
	if pid != group || pid != session {
		t.Fatalf("child %d has process group %d and session %d; serve was not detached", pid, group, session)
	}
	if _, err := connection.Write([]byte{1}); err != nil {
		t.Fatal(err)
	}
	if _, err := io.ReadAll(connection); err != nil {
		t.Fatal(err)
	}
}

// This is the one real-process wiring test for detached startup. The child
// serves until its single test connection acknowledges its ready message.
func TestDetachedHelper(t *testing.T) {
	if len(os.Args) < 3 || os.Args[len(os.Args)-2] != "--" {
		return
	}
	socket := os.Args[len(os.Args)-1]
	listener, err := net.Listen("unix", socket)
	if err != nil {
		t.Fatal(err)
	}
	defer listener.Close()
	listener.(*net.UnixListener).SetDeadline(time.Now().Add(10 * time.Second))
	connection, err := listener.Accept()
	if err != nil {
		t.Fatal(err)
	}
	defer connection.Close()
	connection.SetDeadline(time.Now().Add(10 * time.Second))
	group, err := syscall.Getpgid(0)
	if err != nil {
		t.Fatal(err)
	}
	session, err := syscall.Getsid(0)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := fmt.Fprintf(connection, "%d %d %d\n", os.Getpid(), group, session); err != nil {
		t.Fatal(err)
	}
	var acknowledgement [1]byte
	if _, err := io.ReadFull(connection, acknowledgement[:]); err != nil {
		t.Fatal(err)
	}
}

func TestStartupFailure(t *testing.T) {
	root, err := os.MkdirTemp("/tmp", "gw-")
	if err != nil {
		t.Fatal(err)
	}
	defer os.RemoveAll(root)
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	if connection, err := Dial(ctx, filepath.Join(root, "missing.sock"), "/nonexistent/vibesysd", nil); err == nil {
		connection.Close()
		t.Fatal("missing executable accepted")
	}
}
