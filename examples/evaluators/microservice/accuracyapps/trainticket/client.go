package trainticket

import (
	"context"
	"fmt"
	"net/http"
	"time"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/accuracy/jsoncheck"
	"vibesys/microservice-evaluator/api"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
	"vibesys/microservice-evaluator/wire/httpjson"
)

var listPaths = map[string]string{
	"config": trainticketsupport.MustCollectionSuffix("config"), "station": trainticketsupport.MustCollectionSuffix("station"),
	"train": trainticketsupport.MustCollectionSuffix("train"), "travel": trainticketsupport.MustCollectionSuffix("travel"),
	"route": trainticketsupport.MustCollectionSuffix("route"), "price": trainticketsupport.MustCollectionSuffix("price"),
}

type client struct {
	runtime api.Runtime
	timeout time.Duration
	token   string
}

func newClient(runtime api.Runtime, timeout time.Duration, token string) (*client, error) {
	if token == "" {
		return nil, fmt.Errorf("accuracy client token is empty")
	}
	return &client{runtime: runtime, timeout: timeout, token: token}, nil
}

func (c *client) request(
	ctx context.Context,
	service, method, path string,
	body any,
	authenticated bool,
) api.ProtocolResult {
	authorization := ""
	if authenticated {
		authorization = "Bearer " + c.token
	}
	spec, err := httpjson.Request(method, servicePath(service, path), body, authorization)
	if err != nil {
		return api.ProtocolResult{ErrorCategory: "request_json", ErrorMessage: err.Error()}
	}
	requestContext, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	return c.runtime.Invoke(requestContext, api.Invocation{
		Target: service, Operation: "accuracy", Payload: spec,
	})
}

func servicePath(service, suffix string) string {
	return trainticketsupport.MustPath(service, suffix)
}

func (c *client) envelope(
	ctx context.Context,
	service, method, path string,
	body any,
	httpStatus, appStatus int,
) (any, error) {
	result := c.request(ctx, service, method, path, body, true)
	envelope, err := httpcheck.ExactEnvelope(result, httpStatus, appStatus)
	if err != nil {
		return nil, fmt.Errorf("%s %s%s: %w", method, service, path, err)
	}
	return envelope.Data, nil
}

func (c *client) list(
	ctx context.Context,
	service string,
) (map[string]jsoncheck.Object, error) {
	data, err := c.envelope(ctx, service, http.MethodGet, listPaths[service], nil, 200, 1)
	if err != nil {
		return nil, err
	}
	indexed, err := contracts[service].IndexList(data, service+" list")
	if err != nil {
		return nil, err
	}
	return indexed, nil
}

func (c *client) exactList(
	ctx context.Context,
	service string,
	expected []any,
	where string,
) error {
	data, err := c.envelope(ctx, service, http.MethodGet, listPaths[service], nil, 200, 1)
	if err != nil {
		return err
	}
	if _, err := contracts[service].ExactList(data, expected, where); err != nil {
		return err
	}
	return nil
}
