package hotel

import (
	"fmt"
	"math/rand"
	"net/http"

	"vibesys/microservice-evaluator/accuracy/httpcheck"
	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/wire/httpjson"
)

const loginSuccess = "Login successfully!"

func loginRequest(index int) api.HTTPRequestSpec {
	username, password := MustUser(index)
	request := httpjson.MustRequest(http.MethodGet, "/user", nil, "")
	request.Query = map[string]string{"username": username, "password": password}
	return request
}

func validateLogin(result api.ProtocolResult) error {
	response, err := httpcheck.Response(result, http.StatusOK)
	if err != nil {
		return err
	}
	value, err := httpcheck.DecodeJSON(response.Body)
	if err != nil {
		return err
	}
	object, ok := value.(map[string]any)
	if !ok {
		return fmt.Errorf("login response must be an object, got %T", value)
	}
	if err := httpcheck.ExactFields(object, "message"); err != nil {
		return fmt.Errorf("login response: %w", err)
	}
	message, ok := object["message"].(string)
	if !ok || message != loginSuccess {
		return fmt.Errorf("login message = %v, expected %q", object["message"], loginSuccess)
	}
	return nil
}

func statusOK(result api.ProtocolResult) error {
	_, err := httpcheck.Response(result, http.StatusOK)
	return err
}

func readinessRequest(path string, query map[string]string) api.HTTPRequestSpec {
	request := httpjson.MustRequest(http.MethodGet, path, nil, "")
	request.Query = query
	return request
}

// ReadinessProbes reaches every downstream service used by the canonical
// workload through the frontend rather than accepting the static root page.
func ReadinessProbes(seed int64) []api.ReadinessProbe {
	random := rand.New(rand.NewSource(seed ^ 0x68f07e1))
	index := random.Intn(501)
	probe := func(name string, request api.HTTPRequestSpec, validate func(api.ProtocolResult) error) api.ReadinessProbe {
		return api.ReadinessProbe{
			Name: name,
			Invocation: api.Invocation{
				Target: GatewayTarget, Operation: "preflight", Payload: request,
			},
			Validate: validate,
		}
	}
	return []api.ReadinessProbe{
		probe("readiness-user-service", loginRequest(index), validateLogin),
		probe("readiness-search-services", readinessRequest("/hotels", map[string]string{
			"inDate": "2015-04-09", "outDate": "2015-04-10",
			"lat": "37.7867", "lon": "-122.4112",
		}), statusOK),
		probe("readiness-recommendation-service", readinessRequest("/recommendations", map[string]string{
			"require": "price", "lat": "37.7867", "lon": "-122.4112",
		}), statusOK),
	}
}

// PreflightProbes verifies strict JSON and persistent HTTP with the same
// request sequence in benchmark and accuracy modes.
func PreflightProbes() []api.ReadinessProbe {
	request := loginRequest(0)
	probe := func(name string, validate func(api.ProtocolResult) error) api.ReadinessProbe {
		return api.ReadinessProbe{
			Name: name,
			Invocation: api.Invocation{
				Target: GatewayTarget, Operation: "preflight", Payload: request,
			},
			Validate: validate,
		}
	}
	return []api.ReadinessProbe{
		probe("persistent-http-first", validateLogin),
		probe("persistent-http-second", func(result api.ProtocolResult) error {
			if err := validateLogin(result); err != nil {
				return err
			}
			if !result.ConnectionKnown || !result.ConnectionReused {
				return fmt.Errorf("HTTP connection was not reused for sequential requests")
			}
			return nil
		}),
	}
}
