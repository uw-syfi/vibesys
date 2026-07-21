package httpdriver

import (
	"bytes"
	"context"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/http/httptrace"
	"net/url"
	"strconv"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

const maxResponseBytes = 16 << 20

type Driver struct{}

func New() *Driver {
	return &Driver{}
}

func (d *Driver) Protocol() string {
	return "http"
}

func (d *Driver) Open(_ context.Context, target api.Target) (api.Client, error) {
	if target.SessionPolicy != "reuse" && target.SessionPolicy != "new_per_request" {
		return nil, fmt.Errorf("session_policy must be reuse or new_per_request, got %q", target.SessionPolicy)
	}
	if len(target.Settings) != 0 {
		return nil, fmt.Errorf("HTTP driver does not accept target settings")
	}
	base, err := url.Parse(target.Address)
	if err != nil {
		return nil, fmt.Errorf("parse address: %w", err)
	}
	if base.Scheme != "http" && base.Scheme != "https" {
		return nil, fmt.Errorf("address scheme must be http or https, got %q", base.Scheme)
	}
	if base.Host == "" {
		return nil, fmt.Errorf("address must include a host")
	}
	transport := &http.Transport{
		Proxy:                 http.ProxyFromEnvironment,
		DialContext:           (&net.Dialer{Timeout: 10 * time.Second, KeepAlive: 30 * time.Second}).DialContext,
		ForceAttemptHTTP2:     true,
		MaxIdleConns:          1024,
		MaxIdleConnsPerHost:   1024,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   10 * time.Second,
		ExpectContinueTimeout: time.Second,
		DisableKeepAlives:     target.SessionPolicy == "new_per_request",
	}
	return &client{
		base:      base,
		transport: transport,
		http:      &http.Client{Transport: transport},
	}, nil
}

type client struct {
	base      *url.URL
	transport *http.Transport
	http      *http.Client
}

func (c *client) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	spec, ok := invocation.Payload.(api.HTTPRequestSpec)
	if !ok {
		if pointer, pointerOK := invocation.Payload.(*api.HTTPRequestSpec); pointerOK && pointer != nil {
			spec = *pointer
			ok = true
		}
	}
	if !ok {
		return failure("invalid_request", fmt.Sprintf("HTTP driver received payload %T", invocation.Payload))
	}
	requestURL, err := c.base.Parse(spec.Path)
	if err != nil {
		return failure("invalid_request", fmt.Sprintf("resolve request path: %v", err))
	}
	query := requestURL.Query()
	for key, value := range spec.Query {
		query.Set(key, value)
	}
	requestURL.RawQuery = query.Encode()

	var body io.Reader
	var bodyBytes []byte
	if len(spec.Form) > 0 && spec.Body != "" {
		return failure("invalid_request", "HTTP request cannot set both form and body")
	}
	if len(spec.Form) > 0 {
		values := make(url.Values, len(spec.Form))
		for key, value := range spec.Form {
			values.Set(key, value)
		}
		bodyBytes = []byte(values.Encode())
		body = bytes.NewReader(bodyBytes)
	} else if spec.Body != "" {
		bodyBytes = []byte(spec.Body)
		body = bytes.NewReader(bodyBytes)
	}
	method := strings.ToUpper(spec.Method)
	if method == "" {
		method = http.MethodGet
	}
	request, err := http.NewRequestWithContext(ctx, method, requestURL.String(), body)
	if err != nil {
		return failure("invalid_request", err.Error())
	}
	for key, value := range spec.Headers {
		request.Header.Set(key, value)
	}
	connectionKnown := false
	connectionReused := false
	trace := &httptrace.ClientTrace{GotConn: func(info httptrace.GotConnInfo) {
		connectionKnown = true
		connectionReused = info.Reused
	}}
	request = request.WithContext(httptrace.WithClientTrace(request.Context(), trace))
	if len(spec.Form) > 0 && request.Header.Get("Content-Type") == "" {
		request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}

	response, err := c.http.Do(request)
	if err != nil {
		category := "transport"
		if errors.Is(err, context.DeadlineExceeded) || errors.Is(ctx.Err(), context.DeadlineExceeded) {
			category = "timeout"
		} else if errors.Is(err, context.Canceled) {
			category = "cancelled"
		}
		result := failure(category, err.Error())
		result.RequestBytes = int64(len(bodyBytes))
		return result
	}
	defer response.Body.Close()
	limited := io.LimitReader(response.Body, maxResponseBytes+1)
	responseBody, err := io.ReadAll(limited)
	if err != nil {
		return failure("response_read", err.Error())
	}
	if len(responseBody) > maxResponseBytes {
		return failure("response_too_large", fmt.Sprintf("response exceeds %d bytes", maxResponseBytes))
	}
	return api.ProtocolResult{
		TransportSuccess: true,
		NativeStatus:     strconv.Itoa(response.StatusCode),
		RequestBytes:     int64(len(bodyBytes)),
		ResponseBytes:    int64(len(responseBody)),
		Metadata:         cloneHeader(response.Header),
		Payload: api.HTTPResponse{
			StatusCode: response.StatusCode,
			Body:       responseBody,
		},
		ConnectionKnown:  connectionKnown,
		ConnectionReused: connectionReused,
	}
}

func (c *client) Close() error {
	c.transport.CloseIdleConnections()
	return nil
}

func failure(category string, message string) api.ProtocolResult {
	return api.ProtocolResult{ErrorCategory: category, ErrorMessage: message}
}

func cloneHeader(header http.Header) map[string][]string {
	cloned := make(map[string][]string, len(header))
	for key, values := range header {
		cloned[http.CanonicalHeaderKey(key)] = append([]string(nil), values...)
	}
	return cloned
}
