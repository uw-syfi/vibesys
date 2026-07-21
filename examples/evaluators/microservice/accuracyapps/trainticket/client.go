package trainticket

import (
	"context"
	"crypto/hmac"
	cryptorand "crypto/rand"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"time"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/accuracy/jsoncheck"
	"vibesys/microservice-evaluator/api"
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
	token, err := adminToken(time.Now())
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
	spec := api.HTTPRequestSpec{
		Method: method,
		Path:   servicePaths[service] + path,
		Headers: map[string]string{
			"Accept": "application/json,text/plain,*/*",
		},
	}
	if authenticated {
		spec.Headers["Authorization"] = "Bearer " + c.token
	}
	if body != nil {
		encoded, err := json.Marshal(body)
		if err != nil {
			return api.ProtocolResult{ErrorCategory: "request_json", ErrorMessage: err.Error()}
		}
		spec.Body = string(encoded)
		spec.Headers["Content-Type"] = "application/json"
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

func adminToken(now time.Time) (string, error) {
	identityBytes := make([]byte, 12)
	if _, err := cryptorand.Read(identityBytes); err != nil {
		return "", fmt.Errorf("generate accuracy identity: %w", err)
	}
	identity := hex.EncodeToString(identityBytes)
	headerJSON := "{\"alg\":\"HS256\",\"typ\":\"JWT\"}"
	header := base64.RawURLEncoding.EncodeToString([]byte(headerJSON))
	claimsRaw, err := json.Marshal(map[string]any{
		"sub": identity, "roles": []string{"ROLE_ADMIN"}, "id": identity,
		"iat": now.Unix(), "exp": now.Add(time.Hour).Unix(),
	})
	if err != nil {
		return "", fmt.Errorf("encode accuracy token: %w", err)
	}
	claims := base64.RawURLEncoding.EncodeToString(claimsRaw)
	input := header + "." + claims
	signer := hmac.New(sha256.New, []byte("secret"))
	_, _ = signer.Write([]byte(input))
	signature := base64.RawURLEncoding.EncodeToString(signer.Sum(nil))
	return input + "." + signature, nil
}
