// Package connect ensures a detached gateway exists, then dials its Unix socket.
package connect

import (
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/fsnotify/fsnotify"
	"github.com/uw-syfi/vibesys/gateway/internal/lifecycle"
)

// ServeLogTail bounds what connect reports from a serve candidate's stderr.
const ServeLogTail = 4096

// Dial starts at most one detached serve candidate per invocation. Concurrent
// candidates are serialized by serve's lock. The caller bounds startup via ctx.
// binary and args are the current executable and its serve argument vector.
// The stderr tail a previous candidate left is written to report before a new
// candidate replaces it, and a startup timeout carries the new candidate's.
func Dial(ctx context.Context, socket, binary string, args []string, report io.Writer) (net.Conn, error) {
	if err := lifecycle.ValidateSocketPath(socket); err != nil {
		return nil, err
	}
	dial := func() (net.Conn, error) { return (&net.Dialer{}).DialContext(ctx, "unix", socket) }
	connection, err := dial()
	if err == nil {
		return connection, nil
	}
	if !absent(err) {
		return nil, err
	}
	if err := os.MkdirAll(filepath.Dir(socket), 0700); err != nil {
		return nil, err
	}
	watch, err := fsnotify.NewWatcher()
	if err != nil {
		return nil, err
	}
	defer watch.Close()
	if err := watch.Add(filepath.Dir(socket)); err != nil {
		return nil, err
	}
	// Watch before the second dial, so an existing candidate cannot publish its
	// socket between the observation and the wait.
	connection, err = dial()
	if err == nil {
		return connection, nil
	}
	if !absent(err) {
		return nil, err
	}
	serveLog := ServeLogPath(socket)
	if tail := logTail(serveLog); tail != "" {
		fmt.Fprintf(report, "previous gateway serve stderr:\n%s\n", tail)
	}
	if err := start(binary, args, serveLog); err != nil {
		return nil, err
	}
	guard := time.NewTicker(100 * time.Millisecond)
	defer guard.Stop()
	for {
		connection, err = dial()
		if err == nil {
			return connection, nil
		}
		if !absent(err) {
			return nil, err
		}
		select {
		case <-ctx.Done():
			if tail := logTail(serveLog); tail != "" {
				return nil, fmt.Errorf("gateway startup: %w\nserve stderr:\n%s", ctx.Err(), tail)
			}
			return nil, fmt.Errorf("gateway startup: %w", ctx.Err())
		case _, ok := <-watch.Events:
			if !ok {
				return nil, os.ErrClosed
			}
		case err, ok := <-watch.Errors:
			if !ok {
				return nil, os.ErrClosed
			}
			if !errors.Is(err, fsnotify.ErrEventOverflow) {
				return nil, err
			}
		case <-guard.C:
		}
	}
}

func absent(err error) bool {
	return errors.Is(err, os.ErrNotExist) || errors.Is(err, syscall.ECONNREFUSED)
}

// ServeLogPath keeps a serve candidate's stderr beside its socket. Each start
// truncates it; serve writes only its exit error there.
func ServeLogPath(socket string) string {
	return filepath.Join(filepath.Dir(socket), "serve.log")
}

// logTail is the newest ServeLogTail bytes of path, or "" when it is absent or empty.
func logTail(path string) string {
	file, err := os.Open(path)
	if err != nil {
		return ""
	}
	defer file.Close()
	info, err := file.Stat()
	if err != nil {
		return ""
	}
	offset := max(info.Size()-ServeLogTail, 0)
	tail := make([]byte, info.Size()-offset)
	if _, err := file.ReadAt(tail, offset); err != nil && !errors.Is(err, io.EOF) {
		return ""
	}
	return strings.TrimSpace(string(tail))
}

func start(binary string, args []string, serveLog string) error {
	command := exec.Command(binary, args...)
	command.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	// A daemon must inherit neither SSH's pipes nor its controlling session.
	null, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer null.Close()
	stderr, err := os.OpenFile(serveLog, os.O_CREATE|os.O_TRUNC|os.O_WRONLY, 0600)
	if err != nil {
		return err
	}
	defer stderr.Close()
	command.Stdin = null
	command.Stdout = null
	command.Stderr = stderr
	if err := command.Start(); err != nil {
		return err
	}
	// Reap if connect remains alive. Exiting connect does not terminate serve.
	go func() { _ = command.Wait() }()
	return nil
}
