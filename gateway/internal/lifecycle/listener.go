package lifecycle

import (
	"net"
	"sync"
	"sync/atomic"
)

// Connections counts the connections a Track has open. Unlike the monitor's count, which a live run
// holds open too, it counts nothing else, so it can say whether anyone is connected.
type Connections struct{ open atomic.Int64 }

// Open is how many there are now.
func (c *Connections) Open() uint32 {
	return uint32(max(c.open.Load(), 0))
}

// Track counts accepted sockets, including connections still performing their
// HTTP/2 handshake. Each connection releases its count exactly once on Close.
func Track(listener net.Listener, monitor *Monitor, connections *Connections) net.Listener {
	return &trackedListener{Listener: listener, monitor: monitor, connections: connections}
}

type trackedListener struct {
	net.Listener
	monitor     *Monitor
	connections *Connections
}

func (l *trackedListener) Accept() (net.Conn, error) {
	connection, err := l.Listener.Accept()
	if err != nil {
		return nil, err
	}
	if !l.monitor.Opened() {
		connection.Close()
		return nil, net.ErrClosed
	}
	l.connections.open.Add(1)
	return &trackedConnection{Conn: connection, monitor: l.monitor, connections: l.connections}, nil
}

type trackedConnection struct {
	net.Conn
	once        sync.Once
	monitor     *Monitor
	connections *Connections
}

func (c *trackedConnection) Close() error {
	err := c.Conn.Close()
	c.once.Do(func() {
		c.connections.open.Add(-1)
		c.monitor.Closed()
	})
	return err
}
