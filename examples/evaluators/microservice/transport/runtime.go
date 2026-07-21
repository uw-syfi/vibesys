package transport

import (
	"context"
	"errors"
	"fmt"

	"vibesys/microservice-evaluator/api"
)

type DriverRegistry interface {
	Driver(string) (api.Driver, error)
}

// Runtime owns one protocol client per configured target. Both the benchmark
// engine and the accuracy runner use this implementation, so connection policy,
// transport errors, response limits, and target routing cannot drift.
type Runtime struct {
	clients   map[string]api.Client
	protocols map[string]string
}

func Open(ctx context.Context, registry DriverRegistry, targets []api.Target) (*Runtime, error) {
	runtime := &Runtime{
		clients:   make(map[string]api.Client, len(targets)),
		protocols: make(map[string]string, len(targets)),
	}
	for _, target := range targets {
		driver, err := registry.Driver(target.Protocol)
		if err != nil {
			_ = runtime.Close()
			return nil, fmt.Errorf("target %q: %w", target.Name, err)
		}
		client, err := driver.Open(ctx, target)
		if err != nil {
			_ = runtime.Close()
			return nil, fmt.Errorf("open target %q: %w", target.Name, err)
		}
		runtime.clients[target.Name] = client
		runtime.protocols[target.Name] = target.Protocol
	}
	return runtime, nil
}

func (r *Runtime) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	client, ok := r.clients[invocation.Target]
	if !ok {
		return api.ProtocolResult{
			ErrorCategory: "unknown_target",
			ErrorMessage:  fmt.Sprintf("invocation references unknown target %q", invocation.Target),
		}
	}
	return client.Invoke(ctx, invocation)
}

func (r *Runtime) Protocol(target string) (string, bool) {
	protocol, ok := r.protocols[target]
	return protocol, ok
}

func (r *Runtime) Close() error {
	var closeErrors []error
	for target, client := range r.clients {
		if err := client.Close(); err != nil {
			closeErrors = append(closeErrors, fmt.Errorf("close target %q: %w", target, err))
		}
	}
	return errors.Join(closeErrors...)
}
