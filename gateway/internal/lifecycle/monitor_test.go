package lifecycle

import (
	"net"
	"testing"
	"time"
)

type manualClock struct{ alarms []func() }

func (c *manualClock) AfterFunc(_ time.Duration, fire func()) func() {
	c.alarms = append(c.alarms, fire)
	return func() {}
}

func TestMonitorIgnoresStaleAlarms(t *testing.T) {
	clock := &manualClock{}
	stopped := 0
	monitor := NewMonitor(time.Minute, clock, func() { stopped++ })
	monitor.Opened()
	clock.alarms[0]()
	if stopped != 0 {
		t.Fatal("expired while connected")
	}
	monitor.Closed()
	monitor.Opened()
	clock.alarms[1]()
	if stopped != 0 {
		t.Fatal("expired after reconnect")
	}
	monitor.Closed()
	clock.alarms[2]()
	clock.alarms[2]()
	if stopped != 1 {
		t.Fatalf("stop count = %d", stopped)
	}
	monitor.Stop()
}

func TestMonitorStopInvalidatesExpiry(t *testing.T) {
	clock := &manualClock{}
	monitor := NewMonitor(time.Minute, clock, func() { t.Fatal("expired after stop") })
	monitor.Stop()
	clock.alarms[0]()
}

// What is counted is connections, once each however often they are closed.
func TestConnectionsCountsWhatIsOpen(t *testing.T) {
	server, client := net.Pipe()
	defer client.Close()
	listener := &oneListener{connection: server}
	var connections Connections
	tracked := Track(listener, NewMonitor(time.Minute, &manualClock{}, func() {}), &connections)
	accepted, err := tracked.Accept()
	if err != nil {
		t.Fatal(err)
	}
	if connections.Open() != 1 {
		t.Fatalf("%d open, want 1", connections.Open())
	}
	accepted.Close()
	accepted.Close()
	if connections.Open() != 0 {
		t.Fatalf("%d open, want 0", connections.Open())
	}
}

type oneListener struct {
	net.Listener
	connection net.Conn
}

func (l *oneListener) Accept() (net.Conn, error) { return l.connection, nil }
