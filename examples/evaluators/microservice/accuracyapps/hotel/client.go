package hotel

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/api"
	hotelsupport "vibesys/microservice-evaluator/appsupport/hotel"
	"vibesys/microservice-evaluator/wire/httpjson"
)

type client struct {
	runtime api.Runtime
	timeout time.Duration
}

func (c client) request(ctx context.Context, path string, query map[string]string) api.ProtocolResult {
	spec := httpjson.MustRequest(http.MethodGet, path, nil, "")
	spec.Query = query
	requestContext, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	return c.runtime.Invoke(requestContext, api.Invocation{
		Target: hotelsupport.GatewayTarget, Operation: "accuracy", Payload: spec,
	})
}

func (c client) geoJSON(ctx context.Context, path string, query map[string]string) (map[string]feature, error) {
	result := c.request(ctx, path, query)
	response, err := httpcheck.Response(result, http.StatusOK)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", path, err)
	}
	decoded, err := httpcheck.DecodeJSON(response.Body)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", path, err)
	}
	features, err := decodeFeatureCollection(decoded)
	if err != nil {
		return nil, fmt.Errorf("GET %s: %w", path, err)
	}
	return features, nil
}

func (c client) exactMessage(ctx context.Context, path string, query map[string]string, expected string) error {
	message, err := c.message(ctx, path, query)
	if err != nil {
		return err
	}
	if message != expected {
		return fmt.Errorf("GET %s message = %q, expected %q", path, message, expected)
	}
	return nil
}

func (c client) message(ctx context.Context, path string, query map[string]string) (string, error) {
	result := c.request(ctx, path, query)
	response, err := httpcheck.Response(result, http.StatusOK)
	if err != nil {
		return "", fmt.Errorf("GET %s: %w", path, err)
	}
	decoded, err := httpcheck.DecodeJSON(response.Body)
	if err != nil {
		return "", fmt.Errorf("GET %s: %w", path, err)
	}
	object, ok := decoded.(map[string]any)
	if !ok {
		return "", fmt.Errorf("GET %s response must be an object, got %T", path, decoded)
	}
	if err := httpcheck.ExactFields(object, "message"); err != nil {
		return "", fmt.Errorf("GET %s response: %w", path, err)
	}
	message, ok := object["message"].(string)
	if !ok {
		return "", fmt.Errorf("GET %s message must be a string, got %T", path, object["message"])
	}
	return message, nil
}

func (c client) badRequest(ctx context.Context, path string, query map[string]string) error {
	if _, err := httpcheck.Response(c.request(ctx, path, query), http.StatusBadRequest); err != nil {
		return fmt.Errorf("GET %s negative case: %w", path, err)
	}
	return nil
}
