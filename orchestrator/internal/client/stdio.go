package client

import (
	"io"
	"net"
	"time"
)

// StdioConn makes a net.Conn of a process's pipes: reads come from r, writes go to w. It is how
// `ssh host vibesys-orchestrator connect` becomes a connection. Closing it closes both. Deadlines
// are not supported, which gRPC does not need once connected.
func StdioConn(r io.ReadCloser, w io.WriteCloser) net.Conn { return &stdioConn{r: r, w: w} }

type stdioConn struct {
	r io.ReadCloser
	w io.WriteCloser
}

type stdioAddr struct{}

func (stdioAddr) Network() string { return "stdio" }
func (stdioAddr) String() string  { return "vibesys-orchestrator connect" }

func (c *stdioConn) Read(p []byte) (int, error)  { return c.r.Read(p) }
func (c *stdioConn) Write(p []byte) (int, error) { return c.w.Write(p) }
func (c *stdioConn) Close() error {
	werr, rerr := c.w.Close(), c.r.Close()
	if werr != nil {
		return werr
	}
	return rerr
}
func (c *stdioConn) LocalAddr() net.Addr              { return stdioAddr{} }
func (c *stdioConn) RemoteAddr() net.Addr             { return stdioAddr{} }
func (c *stdioConn) SetDeadline(time.Time) error      { return nil }
func (c *stdioConn) SetReadDeadline(time.Time) error  { return nil }
func (c *stdioConn) SetWriteDeadline(time.Time) error { return nil }
