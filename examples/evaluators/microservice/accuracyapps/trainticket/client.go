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

var servicePaths = map[string]string{
	"config": "/api/v1/configservice", "station": "/api/v1/stationservice",
	"train": "/api/v1/trainservice", "travel": "/api/v1/travelservice",
	"route": "/api/v1/routeservice", "price": "/api/v1/priceservice",
}

var listPaths = map[string]string{
	"config": "/configs", "station": "/stations", "train": "/trains",
	"travel": "/trips", "route": "/routes", "price": "/prices",
}

type client struct {
	runtime api.Runtime
	timeout time.Duration
	token   string
}

func newClient(runtime api.Runtime, timeout time.Duration) (*client, error) {
	token, err := trainticketsupport.AdminToken(time.Now())
	if err != nil {
		return nil, err
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
	spec, err := httpjson.Request(method, servicePaths[service]+path, body, authorization)
	if err != nil {
		return api.ProtocolResult{ErrorCategory: "request_json", ErrorMessage: err.Error()}
	}
	requestContext, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	return c.runtime.Invoke(requestContext, api.Invocation{
		Target: service, Operation: "accuracy", Payload: spec,
	})
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
