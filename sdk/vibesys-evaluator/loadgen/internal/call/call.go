// Package call sends one compiled operation's steps over HTTP/1.1 and reports each step's outcome:
// status, the body or its digest, and the error kind. Captures are taken from each response and
// filled into the later steps' templates; an operation stops at its first failed step.
package call

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/url"
	"slices"
	"strings"
	"time"

	pb "github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/gen/vibesys/evaluator/v1"
	"github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/loadgen/internal/plan"
)

// maxResponseBytes bounds one response body.
const maxResponseBytes = 16 << 20

const maxErrorMessage = 1024

// Client sends operations to one target.
type Client struct {
	target    string
	timeout   time.Duration
	http      *http.Client
	transport *http.Transport
}

// Options configures a Client.
type Options struct {
	// Timeout bounds each step.
	Timeout time.Duration
	// Conns is how many connections may be idle (the clients or max in flight).
	Conns int
	// Fresh opens a new connection for every step (readiness probes).
	Fresh bool
}

// NewClient returns a client for target, an http:// or https:// base URL.
func NewClient(target string, options Options) (*Client, error) {
	base, err := url.Parse(target)
	if err != nil {
		return nil, fmt.Errorf("--target: %w", err)
	}
	if base.Scheme != "http" && base.Scheme != "https" || base.Host == "" {
		return nil, fmt.Errorf("--target must be an http:// or https:// URL with a host, got %q", target)
	}
	if options.Timeout <= 0 {
		return nil, errors.New("--timeout must be positive")
	}
	conns := max(options.Conns, 1024)
	transport := &http.Transport{
		DialContext:           (&net.Dialer{Timeout: options.Timeout, KeepAlive: 30 * time.Second}).DialContext,
		MaxIdleConns:          conns,
		MaxIdleConnsPerHost:   conns,
		IdleConnTimeout:       90 * time.Second,
		TLSHandshakeTimeout:   options.Timeout,
		ExpectContinueTimeout: time.Second,
		DisableKeepAlives:     options.Fresh,
	}
	return &Client{
		target:    strings.TrimSuffix(base.String(), "/"),
		timeout:   options.Timeout,
		transport: transport,
		http: &http.Client{
			Transport:     transport,
			CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse },
		},
	}, nil
}

// Close releases idle connections.
func (c *Client) Close() { c.transport.CloseIdleConnections() }

// Outcome is what happened to one operation.
type Outcome struct {
	Steps []*pb.StepResult
	// Error is ERROR_KIND_UNSPECIFIED when every step succeeded.
	Error pb.ErrorKind
	// Start is when the first step was sent, End when the last one finished.
	Start, End time.Time
}

// Do sends op's steps in order. ctx is the phase: once it ends, a step in flight fails CANCELLED.
func (c *Client) Do(ctx context.Context, op *plan.Op) Outcome {
	var captures map[string]plan.Captured
	outcome := Outcome{Steps: make([]*pb.StepResult, 0, len(op.Steps))}
	for i := range op.Steps {
		step := &op.Steps[i]
		result, response := c.send(ctx, step, captures, op.KeepBodies)
		if i == 0 {
			outcome.Start = time.Unix(0, result.GetStartUnixNanos())
		}
		outcome.End = time.Unix(0, result.GetEndUnixNanos())
		if result.GetError() == pb.ErrorKind_ERROR_KIND_UNSPECIFIED && len(step.Captures) > 0 {
			if captures == nil {
				captures = make(map[string]plan.Captured, len(step.Captures))
			}
			if err := capture(step.Captures, response, captures); err != nil {
				result.Error = pb.ErrorKind_ERROR_KIND_CAPTURE
				result.ErrorMessage = truncate(err.Error())
			}
		}
		outcome.Steps = append(outcome.Steps, result)
		if result.GetError() != pb.ErrorKind_ERROR_KIND_UNSPECIFIED {
			outcome.Error = result.GetError()
			break
		}
	}
	return outcome
}

type response struct {
	header http.Header
	body   []byte
}

func (c *Client) send(ctx context.Context, step *plan.Step, captures map[string]plan.Captured, keep bool) (*pb.StepResult, response) {
	request, err := c.build(ctx, step, captures)
	start := time.Now()
	result := &pb.StepResult{StartUnixNanos: start.UnixNano()}
	if err != nil {
		return fail(result, pb.ErrorKind_ERROR_KIND_TRANSPORT, err), response{}
	}
	stepCtx, cancel := context.WithTimeout(ctx, c.timeout)
	defer cancel()
	reply, err := c.http.Do(request.WithContext(stepCtx))
	if err != nil {
		return fail(result, kindOf(ctx, stepCtx, err), err), response{}
	}
	body, err := io.ReadAll(io.LimitReader(reply.Body, maxResponseBytes+1))
	_ = reply.Body.Close()
	result.Status = uint32(reply.StatusCode)
	if err != nil {
		return fail(result, kindOf(ctx, stepCtx, err), err), response{}
	}
	if len(body) > maxResponseBytes {
		return fail(result, pb.ErrorKind_ERROR_KIND_TRANSPORT, fmt.Errorf("response body exceeds %d bytes", maxResponseBytes)), response{}
	}
	result.EndUnixNanos = time.Now().UnixNano()
	if keep {
		result.Response = &pb.StepResult_Body{Body: body}
	} else {
		sum := sha256.Sum256(body)
		result.Response = &pb.StepResult_Digest{Digest: &pb.BodyDigest{Sha256: sum[:], Length: uint64(len(body))}}
	}
	if !slices.Contains(step.Expect, result.Status) {
		result.Error = pb.ErrorKind_ERROR_KIND_STATUS
	}
	return result, response{header: reply.Header, body: body}
}

func (c *Client) build(ctx context.Context, step *plan.Step, captures map[string]plan.Captured) (*http.Request, error) {
	target := c.target + step.Path.Fill(captures)
	if len(step.Query) > 0 {
		target += "?" + encode(step.Query, captures)
	}
	var body io.Reader
	switch step.Body.Kind {
	case plan.RawBody:
		body = bytes.NewReader(step.Body.Raw)
	case plan.TextBody:
		body = strings.NewReader(step.Body.Text.Fill(captures))
	case plan.FormBody:
		body = strings.NewReader(encode(step.Body.Form, captures))
	case plan.NoBody:
	}
	request, err := http.NewRequestWithContext(ctx, step.Method, target, body)
	if err != nil {
		return nil, err
	}
	for _, header := range step.Headers {
		request.Header.Add(header.Name, header.Value.Fill(captures))
	}
	if step.Body.Kind == plan.FormBody && request.Header.Get("Content-Type") == "" {
		request.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	}
	return request, nil
}

// encode URL-encodes params in their order (url.Values would sort them).
func encode(params []plan.Param, captures map[string]plan.Captured) string {
	var out strings.Builder
	for i, param := range params {
		if i > 0 {
			out.WriteByte('&')
		}
		out.WriteString(url.QueryEscape(param.Name))
		out.WriteByte('=')
		out.WriteString(url.QueryEscape(param.Value.Fill(captures)))
	}
	return out.String()
}

func kindOf(phase, step context.Context, err error) pb.ErrorKind {
	switch {
	case phase.Err() != nil:
		return pb.ErrorKind_ERROR_KIND_CANCELLED
	case errors.Is(step.Err(), context.DeadlineExceeded) || errors.Is(err, context.DeadlineExceeded):
		return pb.ErrorKind_ERROR_KIND_TIMEOUT
	default:
		return pb.ErrorKind_ERROR_KIND_TRANSPORT
	}
}

func fail(result *pb.StepResult, kind pb.ErrorKind, err error) *pb.StepResult {
	result.EndUnixNanos = time.Now().UnixNano()
	result.Error = kind
	result.ErrorMessage = truncate(err.Error())
	return result
}

func truncate(message string) string {
	if len(message) <= maxErrorMessage {
		return message
	}
	return message[:maxErrorMessage]
}

func capture(captures []plan.Capture, reply response, into map[string]plan.Captured) error {
	for _, c := range captures {
		if c.Pointer == nil {
			value := reply.header.Get(c.Header)
			if value == "" {
				return fmt.Errorf("capture %q: no %s header", c.Name, c.Header)
			}
			quoted, _ := json.Marshal(value)
			into[c.Name] = plan.Captured{Text: value, JSON: string(quoted)}
			continue
		}
		value, err := Lookup(reply.body, c.Pointer)
		if err != nil {
			return fmt.Errorf("capture %q: %w", c.Name, err)
		}
		into[c.Name] = value
	}
	return nil
}

// Lookup finds the value at a parsed JSON pointer in a JSON document.
func Lookup(document []byte, pointer []string) (plan.Captured, error) {
	raw := json.RawMessage(bytes.TrimSpace(document))
	for _, token := range pointer {
		switch {
		case len(raw) > 0 && raw[0] == '{':
			var object map[string]json.RawMessage
			if err := json.Unmarshal(raw, &object); err != nil {
				return plan.Captured{}, fmt.Errorf("response is not JSON: %v", err)
			}
			next, ok := object[token]
			if !ok {
				return plan.Captured{}, fmt.Errorf("no member %q", token)
			}
			raw = next
		case len(raw) > 0 && raw[0] == '[':
			var array []json.RawMessage
			if err := json.Unmarshal(raw, &array); err != nil {
				return plan.Captured{}, fmt.Errorf("response is not JSON: %v", err)
			}
			index, ok := arrayIndex(token)
			if !ok || index >= len(array) {
				return plan.Captured{}, fmt.Errorf("no element %q in an array of %d", token, len(array))
			}
			raw = array[index]
		default:
			return plan.Captured{}, fmt.Errorf("cannot descend into %q: not an object or array", token)
		}
	}
	if !json.Valid(raw) {
		return plan.Captured{}, errors.New("response is not JSON")
	}
	var compact bytes.Buffer
	_ = json.Compact(&compact, raw)
	text := compact.String()
	if len(raw) > 0 && raw[0] == '"' {
		var value string
		if err := json.Unmarshal(raw, &value); err != nil {
			return plan.Captured{}, err
		}
		return plan.Captured{Text: value, JSON: text}, nil
	}
	return plan.Captured{Text: text, JSON: text}, nil
}

func arrayIndex(token string) (int, bool) {
	if token == "" || (len(token) > 1 && token[0] == '0') {
		return 0, false
	}
	index := 0
	for _, r := range token {
		if r < '0' || r > '9' || index > 1<<30 {
			return 0, false
		}
		index = index*10 + int(r-'0')
	}
	return index, true
}
