// Package pipe copies an opaque duplex byte stream, closing both ends when
// either direction ends. Callers transfer ownership of both endpoints to Copy.
package pipe

import (
	"context"
	"errors"
	"io"
	"net"
)

// Copy returns only after both copy workers have settled. Closing each endpoint
// must unblock its reads and writes; net.Conn and process pipes meet this rule.
func Copy(ctx context.Context, client io.ReadWriteCloser, upstream net.Conn) error {
	defer client.Close()
	defer upstream.Close()
	results := make(chan error, 2)
	go func() { _, err := io.Copy(upstream, client); results <- err }()
	go func() { _, err := io.Copy(client, upstream); results <- err }()
	var first error
	cancelled := false
	select {
	case first = <-results:
	case <-ctx.Done():
		first = ctx.Err()
		cancelled = true
	}
	client.Close()
	upstream.Close()
	// Cancellation consumed no worker result.
	if cancelled {
		<-results
	}
	<-results
	if errors.Is(first, net.ErrClosed) || errors.Is(first, io.ErrClosedPipe) {
		return nil
	}
	return first
}
