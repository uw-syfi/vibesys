// Package connect ensures a detached gateway exists, then dials its Unix socket.
package connect

import (
	"context"
	"errors"
	"fmt"
	"net"
	"os"
	"os/exec"
	"path/filepath"
	"syscall"
	"time"

	"github.com/fsnotify/fsnotify"
)

// Dial starts at most one detached serve candidate per invocation. Concurrent
// candidates are serialized by serve's lock. The caller bounds startup via ctx.
// binary and args are the current executable and its serve argument vector.
func Dial(ctx context.Context, socket, binary string, args []string) (net.Conn, error) {
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
	if err := start(binary, args); err != nil {
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
			return nil, fmt.Errorf("gateway startup: %w", ctx.Err())
		case <-watch.Events:
		case err := <-watch.Errors:
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

func start(binary string, args []string) error {
	command := exec.Command(binary, args...)
	command.SysProcAttr = &syscall.SysProcAttr{Setsid: true}
	// A daemon must inherit neither SSH's pipes nor its controlling session.
	null, err := os.OpenFile(os.DevNull, os.O_RDWR, 0)
	if err != nil {
		return err
	}
	defer null.Close()
	command.Stdin = null
	command.Stdout = null
	command.Stderr = null
	if err := command.Start(); err != nil {
		return err
	}
	// Reap if connect remains alive. Exiting connect does not terminate serve.
	go func() { _ = command.Wait() }()
	return nil
}
