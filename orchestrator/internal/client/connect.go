package client

import (
	"context"
	"errors"
	"io"
	"net"
)

// Connect pipes a client's stdio to the daemon: bytes from in go to the daemon's connection and
// bytes from it go to out, until either side ends or ctx does. It is what
// `vibesys-host connect` runs, so that `ssh host vibesys-host connect` is a
// connection to the daemon on that host. It understands none of what it carries.
//
// It returns nil when the client closes its input, and the first error otherwise. Both ends are
// closed on return, so a cut on either side ends the other.
func Connect(ctx context.Context, dial Dialer, in io.Reader, out io.Writer) error {
	conn, err := dial(ctx)
	if err != nil {
		return err
	}
	defer conn.Close()
	return pipe(ctx, conn, in, out)
}

func pipe(ctx context.Context, conn net.Conn, in io.Reader, out io.Writer) error {
	ctx, cancel := context.WithCancel(ctx)
	defer cancel()
	results := make(chan error, 2)
	go func() {
		_, err := io.Copy(conn, in)
		results <- err
	}()
	go func() {
		_, err := io.Copy(out, conn)
		results <- err
	}()
	var first error
	select {
	case first = <-results:
	case <-ctx.Done():
		first = ctx.Err()
	}
	// Closing the connection unblocks the copy reading it. The copy reading in may stay blocked on
	// a terminal that never closes; it ends with the process, which is what returning here means.
	_ = conn.Close()
	if closer, ok := in.(io.Closer); ok {
		_ = closer.Close()
	}
	if errors.Is(first, net.ErrClosed) || errors.Is(first, io.ErrClosedPipe) {
		return nil
	}
	return first
}
