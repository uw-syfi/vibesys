package lifecycle

import (
	"net"
	"sync"
)

// Track counts accepted sockets, including connections still performing their
// HTTP/2 handshake. Each connection releases its count exactly once on Close.
func Track(listener net.Listener, monitor *Monitor) net.Listener {
	return &trackedListener{Listener: listener, monitor: monitor}
}

type trackedListener struct {
	net.Listener
	monitor *Monitor
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
	return &trackedConnection{Conn: connection, monitor: l.monitor}, nil
}

type trackedConnection struct {
	net.Conn
	once    sync.Once
	monitor *Monitor
}

func (c *trackedConnection) Close() error {
	err := c.Conn.Close()
	c.once.Do(c.monitor.Closed)
	return err
}
