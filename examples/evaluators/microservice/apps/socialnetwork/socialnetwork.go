package socialnetwork

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strconv"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

const (
	userTimelineRead = "user_timeline_read"
	homeTimelineRead = "home_timeline_read"
	composePost      = "compose_post"
)

type Config struct {
	Users            int
	SeedPostsPerUser int
	UserIDBase       int
	UsernamePrefix   string
	SetupDelay       time.Duration
}

type Application struct {
	config Config
}

func New(workload api.Workload) (api.Application, error) {
	if workload.Load.Repetitions > 1 {
		return nil, fmt.Errorf("Social Network has no topology-neutral reset API; repetitions must be 1 and clean deployments must be compared across runs")
	}
	gatewayFound := false
	for _, target := range workload.Targets {
		if target.Name == "gateway" {
			gatewayFound = true
			if target.Protocol != "http" {
				return nil, fmt.Errorf("Social Network gateway target must use HTTP, got %q", target.Protocol)
			}
		}
	}
	if !gatewayFound {
		return nil, fmt.Errorf("Social Network requires a target named gateway")
	}
	config := Config{
		Users:            50,
		SeedPostsPerUser: 10,
		UserIDBase:       700000,
		UsernamePrefix:   "rbnch_",
		SetupDelay:       3 * time.Second,
	}
	allowed := map[string]bool{
		"users": true, "seed_posts_per_user": true, "user_id_base": true,
		"username_prefix": true, "setup_delay_seconds": true,
	}
	for key := range workload.ApplicationConfig {
		if !allowed[key] {
			return nil, fmt.Errorf("unknown application_config field %q", key)
		}
	}
	var err error
	if config.Users, err = integer(workload.ApplicationConfig, "users", config.Users); err != nil {
		return nil, err
	}
	if config.SeedPostsPerUser, err = integer(workload.ApplicationConfig, "seed_posts_per_user", config.SeedPostsPerUser); err != nil {
		return nil, err
	}
	if config.UserIDBase, err = integer(workload.ApplicationConfig, "user_id_base", config.UserIDBase); err != nil {
		return nil, err
	}
	if value, ok := workload.ApplicationConfig["username_prefix"]; ok {
		prefix, stringOK := value.(string)
		if !stringOK || prefix == "" {
			return nil, fmt.Errorf("application_config.username_prefix must be a non-empty string")
		}
		config.UsernamePrefix = prefix
	}
	if value, ok := workload.ApplicationConfig["setup_delay_seconds"]; ok {
		seconds, numberOK := number(value)
		if !numberOK || seconds < 0 {
			return nil, fmt.Errorf("application_config.setup_delay_seconds must be a non-negative number")
		}
		config.SetupDelay = time.Duration(seconds * float64(time.Second))
	}
	if config.Users <= 0 || config.SeedPostsPerUser < 0 {
		return nil, fmt.Errorf("application_config users must be positive and seed_posts_per_user must be non-negative")
	}
	for _, operation := range workload.Operations {
		if operation.Target != "gateway" {
			return nil, fmt.Errorf("Social Network operation %q must target gateway", operation.Name)
		}
		switch operation.Name {
		case userTimelineRead, homeTimelineRead, composePost:
		default:
			return nil, fmt.Errorf("unknown Social Network operation %q", operation.Name)
		}
	}
	return &Application{config: config}, nil
}

func (a *Application) Name() string {
	return "social-network"
}

func (a *Application) Reset(context.Context, api.Runtime, api.TrialContext) error {
	// DeathStarBench does not expose a topology-neutral reset API. Scenario
	// workloads therefore default to one repetition; clean multi-trial runs must
	// provide an external fresh deployment until such an API exists.
	return nil
}

func (a *Application) Prepare(ctx context.Context, runtime api.Runtime, _ api.TrialContext) (any, error) {
	for index := 0; index < a.config.Users; index++ {
		if err := invokeOK(ctx, runtime, api.HTTPRequestSpec{
			Method: "POST",
			Path:   "/wrk2-api/user/register",
			Form: map[string]string{
				"username":   a.username(index),
				"password":   "rbnch_pass",
				"user_id":    strconv.Itoa(a.userID(index)),
				"first_name": "RB",
				"last_name":  strconv.Itoa(index),
			},
		}); err != nil {
			return nil, fmt.Errorf("register benchmark user %d: %w", index, err)
		}
		if index > 0 {
			if err := a.follow(ctx, runtime, index, index-1); err != nil {
				return nil, err
			}
		}
	}
	if a.config.Users > 1 {
		if err := a.follow(ctx, runtime, 0, a.config.Users-1); err != nil {
			return nil, err
		}
	}
	for user := 0; user < a.config.Users; user++ {
		for post := 0; post < a.config.SeedPostsPerUser; post++ {
			if err := invokeComposeSetup(ctx, runtime, a.composeRequest(user, "seed_"+strconv.Itoa(post))); err != nil {
				return nil, fmt.Errorf("seed post %d for user %d: %w", post, user, err)
			}
		}
	}
	if a.config.SetupDelay > 0 {
		timer := time.NewTimer(a.config.SetupDelay)
		defer timer.Stop()
		select {
		case <-ctx.Done():
			return nil, ctx.Err()
		case <-timer.C:
		}
	}
	return nil, nil
}

func (a *Application) BuildOperation(operation api.Operation, sample api.Sample, _ any) (api.OperationPlan, error) {
	user := int(sample.Random % uint64(a.config.Users))
	var request api.HTTPRequestSpec
	switch operation.Name {
	case userTimelineRead:
		request = api.HTTPRequestSpec{
			Method: "GET",
			Path:   "/wrk2-api/user-timeline/read",
			Query: map[string]string{
				"user_id": strconv.Itoa(a.userID(user)), "start": "0", "stop": "10",
			},
		}
	case homeTimelineRead:
		request = api.HTTPRequestSpec{
			Method: "GET",
			Path:   "/wrk2-api/home-timeline/read",
			Query: map[string]string{
				"user_id": strconv.Itoa(a.userID(user)), "start": "0", "stop": "10",
			},
		}
	case composePost:
		request = a.composeRequest(user, "live_"+strconv.FormatInt(sample.Counter, 10))
	default:
		return api.OperationPlan{}, fmt.Errorf("unknown Social Network operation %q", operation.Name)
	}
	return api.OperationPlan{Invocations: []api.Invocation{{
		Target: operation.Target, Operation: operation.Name, Payload: request,
	}}}, nil
}

func (a *Application) ValidateOperation(operation api.Operation, _ api.OperationPlan, results []api.ProtocolResult) api.ValidationResult {
	if len(results) != 1 {
		return invalid("invalid_response", fmt.Sprintf("expected one protocol result, got %d", len(results)))
	}
	result := results[0]
	if !result.TransportSuccess {
		return api.ValidationResult{ErrorCategory: result.ErrorCategory, ErrorMessage: result.ErrorMessage}
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return invalid("invalid_response", fmt.Sprintf("expected HTTP response, got %T", result.Payload))
	}
	if response.StatusCode != http.StatusOK {
		return invalid("http_status", fmt.Sprintf("unexpected HTTP status %d", response.StatusCode))
	}
	if operation.Name == userTimelineRead || operation.Name == homeTimelineRead {
		var payload any
		if err := json.Unmarshal(response.Body, &payload); err != nil {
			return invalid("response_json", err.Error())
		}
	}
	custom := make(map[string]time.Duration)
	for _, capture := range operation.CaptureHeaders {
		values := result.Metadata[http.CanonicalHeaderKey(capture.Header)]
		if len(values) == 0 {
			continue
		}
		milliseconds, err := strconv.ParseFloat(values[0], 64)
		if err != nil {
			return invalid("timing_header", fmt.Sprintf("header %s is not numeric: %v", capture.Header, err))
		}
		custom[capture.Name] = time.Duration(milliseconds * float64(time.Millisecond))
	}
	return api.ValidationResult{Success: true, CustomTimings: custom}
}

func (a *Application) FinishOperation(api.OperationPlan) {}

func (a *Application) follow(ctx context.Context, runtime api.Runtime, user int, followee int) error {
	err := invokeOK(ctx, runtime, api.HTTPRequestSpec{
		Method: "POST",
		Path:   "/wrk2-api/user/follow",
		Form: map[string]string{
			"user_name":     a.username(user),
			"followee_name": a.username(followee),
		},
	})
	if err != nil {
		return fmt.Errorf("follow user %d -> %d: %w", user, followee, err)
	}
	return nil
}

func (a *Application) composeRequest(user int, text string) api.HTTPRequestSpec {
	return api.HTTPRequestSpec{
		Method: "POST",
		Path:   "/wrk2-api/post/compose",
		Form: map[string]string{
			"username": a.username(user), "user_id": strconv.Itoa(a.userID(user)),
			"text": text, "media_ids": "[]", "media_types": "[]", "post_type": "0",
		},
	}
}

func (a *Application) username(index int) string {
	return a.config.UsernamePrefix + strconv.Itoa(index)
}

func (a *Application) userID(index int) int {
	return a.config.UserIDBase + index
}

func invokeOK(ctx context.Context, runtime api.Runtime, request api.HTTPRequestSpec) error {
	requestContext, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	result := runtime.Invoke(requestContext, api.Invocation{Target: "gateway", Operation: "setup", Payload: request})
	if !result.TransportSuccess {
		return fmt.Errorf("%s: %s", result.ErrorCategory, result.ErrorMessage)
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return fmt.Errorf("expected HTTP response, got %T", result.Payload)
	}
	if response.StatusCode != http.StatusOK {
		body := strings.TrimSpace(string(response.Body))
		if len(body) > 120 {
			body = body[:120]
		}
		return fmt.Errorf("HTTP %d: %s", response.StatusCode, body)
	}
	return nil
}

func invokeComposeSetup(ctx context.Context, runtime api.Runtime, request api.HTTPRequestSpec) error {
	requestContext, cancel := context.WithTimeout(ctx, 10*time.Second)
	defer cancel()
	result := runtime.Invoke(requestContext, api.Invocation{Target: "gateway", Operation: "setup", Payload: request})
	if !result.TransportSuccess {
		return fmt.Errorf("%s: %s", result.ErrorCategory, result.ErrorMessage)
	}
	response, ok := result.Payload.(api.HTTPResponse)
	if !ok {
		return fmt.Errorf("expected HTTP response, got %T", result.Payload)
	}
	if response.StatusCode == http.StatusOK {
		return nil
	}
	// DeathStarBench can report a first-fan-out ZADD error after persisting the
	// post to the user timeline. The legacy setup ignored this known condition;
	// retain that behavior narrowly for fixture creation, not measured writes.
	if response.StatusCode == http.StatusInternalServerError && strings.Contains(string(response.Body), "ZADD") {
		return nil
	}
	return fmt.Errorf("HTTP %d: %s", response.StatusCode, strings.TrimSpace(string(response.Body)))
}

func invalid(category string, message string) api.ValidationResult {
	return api.ValidationResult{ErrorCategory: category, ErrorMessage: message}
}

func integer(values map[string]any, key string, defaultValue int) (int, error) {
	value, ok := values[key]
	if !ok {
		return defaultValue, nil
	}
	numeric, ok := number(value)
	if !ok || numeric != float64(int(numeric)) {
		return 0, fmt.Errorf("application_config.%s must be an integer", key)
	}
	return int(numeric), nil
}

func number(value any) (float64, bool) {
	switch typed := value.(type) {
	case int64:
		return float64(typed), true
	case int:
		return float64(typed), true
	case float64:
		return typed, true
	default:
		return 0, false
	}
}
