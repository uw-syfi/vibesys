package trainticket

import (
	"fmt"
	"math/rand"
	"net/http"
	"strings"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/wire/httpjson"
)

var welcomeEndpoints = map[string]struct {
	suffix string
	text   string
}{
	"config":  {"/welcome", "Welcome to [ Config Service ] !"},
	"station": {"/welcome", "Welcome to [ Station Service ] !"},
	"train":   {"/trains/welcome", "Welcome to [ Train Service ] !"},
	"travel":  {"/welcome", "Welcome to [ Travel Service ] !"},
	"route":   {"/welcome", "Welcome to [ Route Service ] !"},
	"price":   {"/prices/welcome", "Welcome to [ Price Service ] !"},
}

// ReadinessProbes returns the same randomized, unauthenticated readiness plan
// to benchmark and accuracy modes. The seed changes the visible ordering while
// still allowing reproducible runs.
func ReadinessProbes(seed int64) []api.ReadinessProbe {
	ordered := Services()
	random := rand.New(rand.NewSource(seed ^ 0x5a17e55))
	random.Shuffle(len(ordered), func(left, right int) {
		ordered[left], ordered[right] = ordered[right], ordered[left]
	})
	probes := make([]api.ReadinessProbe, 0, len(ordered))
	for _, service := range ordered {
		service := service
		welcome := welcomeEndpoints[service]
		probes = append(probes, api.ReadinessProbe{
			Name: "readiness-" + service,
			Invocation: api.Invocation{
				Target:    service,
				Operation: "preflight",
				Payload: httpjson.MustRequest(
					http.MethodGet,
					MustPath(service, welcome.suffix),
					nil,
					"",
				),
			},
			Validate: func(result api.ProtocolResult) error {
				return httpcheck.ExactText(result, http.StatusOK, welcome.text)
			},
		})
	}
	return probes
}

// PreflightProbes exercises protocol details that are required by both modes.
// These probes deliberately use the same canonical request builder and auth
// grammar as normal traffic so accuracy mode has no unique protocol preamble.
func PreflightProbes(token string) []api.ReadinessProbe {
	path := MustCollectionPath("station")
	authorization := "Bearer " + token
	request := func(name string, validate func(api.ProtocolResult) error) api.ReadinessProbe {
		return api.ReadinessProbe{
			Name: name,
			Invocation: api.Invocation{
				Target:    "station",
				Operation: "preflight",
				Payload:   httpjson.MustRequest(http.MethodGet, path, nil, authorization),
			},
			Validate: validate,
		}
	}
	strictEnvelope := func(result api.ProtocolResult) error {
		_, err := httpcheck.ExactEnvelope(result, http.StatusOK, 1)
		return err
	}
	return []api.ReadinessProbe{
		request("mutable-cache-policy", func(result api.ProtocolResult) error {
			if err := strictEnvelope(result); err != nil {
				return err
			}
			cacheControl := strings.ToLower(httpcheck.Header(result, "Cache-Control"))
			if strings.Contains(cacheControl, "public") || strings.Contains(cacheControl, "immutable") {
				return fmt.Errorf("mutable collection has unsafe Cache-Control %q", cacheControl)
			}
			return nil
		}),
		request("persistent-http-first", strictEnvelope),
		request("persistent-http-second", func(result api.ProtocolResult) error {
			if err := strictEnvelope(result); err != nil {
				return err
			}
			if !result.ConnectionKnown || !result.ConnectionReused {
				return fmt.Errorf("HTTP connection was not reused for sequential requests")
			}
			return nil
		}),
	}
}
