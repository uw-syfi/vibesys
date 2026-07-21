package socialnetwork

import (
	"context"
	"crypto/sha256"
	"encoding/binary"
	"fmt"
	"math"
	"net/http"
	"strconv"
	"strings"
	"time"

	"vibesys/microservice-evaluator/api"
)

const (
	userTimelineRead    = "user_timeline_read"
	homeTimelineRead    = "home_timeline_read"
	composeUserTimeline = "compose_user_timeline"
	legacyComposePost   = "compose_post"
)

type Config struct {
	Users            int
	SeedPostsPerUser int
	UserIDBase       int
	UsernamePrefix   string
	TimelineLimit    int
	SetupDelay       time.Duration
}

type Application struct {
	config    Config
	needsHome bool
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
		TimelineLimit:    10,
		SetupDelay:       3 * time.Second,
	}
	allowed := map[string]bool{
		"users": true, "seed_posts_per_user": true, "user_id_base": true,
		"username_prefix": true, "timeline_limit": true, "setup_delay_seconds": true,
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
	if config.TimelineLimit, err = integer(workload.ApplicationConfig, "timeline_limit", config.TimelineLimit); err != nil {
		return nil, err
	}
	if value, ok := workload.ApplicationConfig["username_prefix"]; ok {
		prefix, stringOK := value.(string)
		if !stringOK || strings.TrimSpace(prefix) == "" {
			return nil, fmt.Errorf("application_config.username_prefix must be a non-empty string")
		}
		config.UsernamePrefix = prefix
	}
	if value, ok := workload.ApplicationConfig["setup_delay_seconds"]; ok {
		seconds, numberOK := number(value)
		delay, valid := checkedDuration(seconds, time.Second)
		if !numberOK || !valid {
			return nil, fmt.Errorf("application_config.setup_delay_seconds must be a non-negative number")
		}
		config.SetupDelay = delay
	}
	if config.UserIDBase <= 0 {
		return nil, fmt.Errorf("application_config.user_id_base must be positive")
	}
	const maximumSafeInteger = 1<<53 - 1
	if config.UserIDBase > maximumSafeInteger-config.Users {
		return nil, fmt.Errorf("application_config.user_id_base is too large for namespaced user IDs")
	}
	if config.TimelineLimit <= 0 {
		return nil, fmt.Errorf("application_config.timeline_limit must be positive")
	}
	needsHome := false
	needsSeedPosts := false
	for _, operation := range workload.Operations {
		if operation.Target != "gateway" {
			return nil, fmt.Errorf("Social Network operation %q must target gateway", operation.Name)
		}
		if operation.HTTP != nil {
			return nil, fmt.Errorf("Social Network operation %q must not declare operations.http", operation.Name)
		}
		switch operation.Name {
		case userTimelineRead:
			needsSeedPosts = true
		case homeTimelineRead, composeUserTimeline:
			needsHome = true
			needsSeedPosts = true
		case legacyComposePost:
			needsHome = true
		default:
			return nil, fmt.Errorf("unknown Social Network operation %q", operation.Name)
		}
		for _, capture := range operation.CaptureHeaders {
			if _, ok := timingInvocation(operation.Name, capture.Header); !ok {
				return nil, fmt.Errorf("operation %q capture header %q is not emitted by one of its protocol steps", operation.Name, capture.Header)
			}
		}
	}
	minimumUsers := 1
	if needsHome {
		minimumUsers = 2
	}
	if config.Users < minimumUsers {
		return nil, fmt.Errorf("application_config.users must be at least %d for the selected operations", minimumUsers)
	}
	if config.SeedPostsPerUser < 0 || (needsSeedPosts && config.SeedPostsPerUser == 0) {
		return nil, fmt.Errorf("application_config.seed_posts_per_user must be positive for timeline reads")
	}
	return &Application{config: config, needsHome: needsHome}, nil
}

func (a *Application) Name() string { return "social-network" }

func (a *Application) SupportsSkipPrepare() bool { return false }

func (a *Application) Reset(context.Context, api.Runtime, api.TrialContext) error {
	// DeathStarBench does not expose a topology-neutral user or post deletion
	// API. Each trial uses a seed-derived namespace so later optimization runs
	// never reuse fixture identities, but independent repetitions still require
	// fresh deployments for comparable cache and database state.
	return nil
}

func (a *Application) Prepare(ctx context.Context, runtime api.Runtime, trial api.TrialContext) (any, error) {
	data := a.makeDataset(trial)
	for index := range data.users {
		user := &data.users[index]
		request := api.HTTPRequestSpec{
			Method: http.MethodPost,
			Path:   "/wrk2-api/user/register",
			Form: map[string]string{
				"username": user.username, "password": "rbnch_pass",
				"user_id": strconv.Itoa(user.id), "first_name": "RB", "last_name": strconv.Itoa(index),
			},
		}
		if err := invokeSetup(ctx, runtime, request, false); err != nil {
			return nil, fmt.Errorf("register benchmark user %d: %w", index, err)
		}
	}
	if a.needsHome {
		for index := range data.users {
			if err := a.follow(ctx, runtime, &data.users[index], &data.users[data.users[index].followee]); err != nil {
				return nil, err
			}
		}
	}
	for userIndex := range data.users {
		for post := 0; post < a.config.SeedPostsPerUser; post++ {
			text := fmt.Sprintf("seed_%s_%d_%d", data.namespace, userIndex, post)
			if err := invokeSetup(ctx, runtime, a.composeRequest(&data.users[userIndex], text), true); err != nil {
				return nil, fmt.Errorf("seed post %d for user %d: %w", post, userIndex, err)
			}
			data.users[userIndex].expected = prependExpected(text, data.users[userIndex].expected)
		}
	}
	if err := waitFor(ctx, a.config.SetupDelay); err != nil {
		return nil, err
	}
	if err := a.verifyFixture(ctx, runtime, data); err != nil {
		return nil, err
	}
	return data, nil
}

func (a *Application) makeDataset(trial api.TrialContext) *dataset {
	fixtureSeed := trial.FixtureSeed
	digest := sha256.Sum256([]byte(fmt.Sprintf("%d/%d", fixtureSeed, trial.Index)))
	namespace := fmt.Sprintf("%x", digest[:6])
	// Allocate an aligned block in JavaScript's exactly representable integer
	// range. Alignment means distinct blocks can never partially overlap.
	const maximumSafeInteger = 1<<53 - 1
	blockCount := uint64((maximumSafeInteger - a.config.UserIDBase + 1) / a.config.Users)
	block := binary.BigEndian.Uint64(digest[6:14]) % blockCount
	offset := int(block) * a.config.Users
	users := make([]benchmarkUser, a.config.Users)
	for index := range users {
		users[index] = benchmarkUser{
			id:       a.config.UserIDBase + offset + index,
			username: fmt.Sprintf("%s%s_%d", a.config.UsernamePrefix, namespace, index),
			followee: (index - 1 + len(users)) % len(users),
			follower: (index + 1) % len(users),
		}
	}
	return &dataset{namespace: namespace, users: users}
}

func (a *Application) verifyFixture(ctx context.Context, runtime api.Runtime, data *dataset) error {
	operation := api.Operation{Name: "setup_verify", Target: "gateway"}
	for index := range data.users {
		user := &data.users[index]
		result := runtime.Invoke(ctx, a.timelineInvocation(operation, userTimelineRead, user))
		if validation := validateTimeline(result, user, user.expected, a.config.TimelineLimit, true); !validation.Success {
			return fmt.Errorf("verify user timeline %d: %s: %s", index, validation.ErrorCategory, validation.ErrorMessage)
		}
	}
	if !a.needsHome {
		return nil
	}
	for index := range data.users {
		user := &data.users[index]
		followee := &data.users[user.followee]
		result := runtime.Invoke(ctx, a.timelineInvocation(operation, homeTimelineRead, user))
		if validation := validateTimeline(result, followee, followee.expected, a.config.TimelineLimit, false); !validation.Success {
			return fmt.Errorf("verify home timeline %d: %s: %s", index, validation.ErrorCategory, validation.ErrorMessage)
		}
	}
	return nil
}

func (a *Application) follow(ctx context.Context, runtime api.Runtime, user, followee *benchmarkUser) error {
	request := api.HTTPRequestSpec{
		Method: http.MethodPost,
		Path:   "/wrk2-api/user/follow",
		Form: map[string]string{
			"user_name": user.username, "followee_name": followee.username,
		},
	}
	if err := invokeSetup(ctx, runtime, request, false); err != nil {
		return fmt.Errorf("follow user %s -> %s: %w", user.username, followee.username, err)
	}
	return nil
}

func (a *Application) composeRequest(user *benchmarkUser, text string) api.HTTPRequestSpec {
	return api.HTTPRequestSpec{
		Method: http.MethodPost,
		Path:   "/wrk2-api/post/compose",
		Form: map[string]string{
			"username": user.username, "user_id": strconv.Itoa(user.id), "text": text,
			"media_ids": "[]", "media_types": "[]", "post_type": "0",
		},
	}
}

func invokeSetup(ctx context.Context, runtime api.Runtime, request api.HTTPRequestSpec, allowPersistedZADD bool) error {
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
	body := strings.TrimSpace(string(response.Body))
	if response.StatusCode == http.StatusOK {
		if request.Path == "/wrk2-api/post/compose" {
			if body != composeSuccessBody {
				return fmt.Errorf("HTTP 200 with unexpected compose response %q", body)
			}
		} else if !strings.Contains(body, "Success") {
			return fmt.Errorf("HTTP 200 with unsuccessful setup response %q", body)
		}
		return nil
	}
	// DeathStarBench can report a first-fan-out ZADD error after persisting a
	// post to the user timeline. Tolerate it only while creating seed fixtures;
	// measured writes still require an ordinary successful response.
	if allowPersistedZADD && response.StatusCode == http.StatusInternalServerError && strings.Contains(body, "ZADD") {
		return nil
	}
	if len(body) > 120 {
		body = body[:120]
	}
	return fmt.Errorf("HTTP %d: %s", response.StatusCode, body)
}

func waitFor(ctx context.Context, delay time.Duration) error {
	if delay <= 0 {
		return nil
	}
	timer := time.NewTimer(delay)
	defer timer.Stop()
	select {
	case <-ctx.Done():
		return ctx.Err()
	case <-timer.C:
		return nil
	}
}

func checkedDuration(value float64, unit time.Duration) (time.Duration, bool) {
	if math.IsNaN(value) || math.IsInf(value, 0) || value < 0 {
		return 0, false
	}
	scaled := value * float64(unit)
	if math.IsInf(scaled, 0) || scaled >= float64(math.MaxInt64) {
		return 0, false
	}
	return time.Duration(scaled), true
}

func prependExpected(text string, values []expectedPost) []expectedPost {
	result := make([]expectedPost, len(values)+1)
	result[0] = expectedPost{text: text}
	copy(result[1:], values)
	return result
}

func integer(values map[string]any, key string, defaultValue int) (int, error) {
	value, ok := values[key]
	if !ok {
		return defaultValue, nil
	}
	switch typed := value.(type) {
	case int:
		return typed, nil
	case int64:
		if typed < int64(math.MinInt) || typed > int64(math.MaxInt) {
			break
		}
		return int(typed), nil
	case float64:
		if !math.IsNaN(typed) && !math.IsInf(typed, 0) && math.Trunc(typed) == typed && typed >= float64(math.MinInt) && typed < -float64(math.MinInt) {
			return int(typed), nil
		}
	}
	return 0, fmt.Errorf("application_config.%s must be an integer", key)
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
