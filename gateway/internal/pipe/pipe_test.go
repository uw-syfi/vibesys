package pipe

import (
	"bytes"
	"context"
	"io"
	"net"
	"testing"
	"time"
)

func TestDuplexAndClosure(t *testing.T) {
	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	client, front := net.Pipe()
	back, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	done := make(chan error, 1)
	go func() { done <- Copy(ctx, front, back) }()
	payload := bytes.Repeat([]byte{0, 255, 10, 13}, 4096)
	writes := make(chan error, 1)
	go func() { _, err := client.Write(payload); writes <- err }()
	got := make([]byte, len(payload))
	server.SetDeadline(time.Now().Add(10 * time.Second))
	client.SetDeadline(time.Now().Add(10 * time.Second))
	if _, err := io.ReadFull(server, got); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatal("upstream changed bytes")
	}
	select {
	case err := <-writes:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	go func() { _, err := server.Write(payload); writes <- err }()
	if _, err := io.ReadFull(client, got); err != nil {
		t.Fatal(err)
	}
	if !bytes.Equal(got, payload) {
		t.Fatal("downstream changed bytes")
	}
	select {
	case err := <-writes:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
	client.Close()
	select {
	case err := <-done:
		if err != nil {
			t.Fatal(err)
		}
	case <-ctx.Done():
		t.Fatal(ctx.Err())
	}
}

func TestCancelSettlesBothWorkers(t *testing.T) {
	ctx, cancel := context.WithCancel(context.Background())
	client, front := net.Pipe()
	back, server := net.Pipe()
	defer client.Close()
	defer server.Close()
	done := make(chan error, 1)
	go func() { done <- Copy(ctx, front, back) }()
	cancel()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("copy hung")
	}
}
