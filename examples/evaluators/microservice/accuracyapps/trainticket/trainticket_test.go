package trainticket

import (
	"context"
	"encoding/base64"
	"encoding/json"
	"math/rand"
	"net/http"
	"net/url"
	"strings"
	"testing"
	"time"

	"vibesys/microservice-evaluator/accuracy"
	"vibesys/microservice-evaluator/api"
	trainticketsupport "vibesys/microservice-evaluator/appsupport/trainticket"
)

type runtimeFunc func(context.Context, api.Invocation) api.ProtocolResult

func (function runtimeFunc) Invoke(ctx context.Context, invocation api.Invocation) api.ProtocolResult {
	return function(ctx, invocation)
}

func envelopeResult(httpStatus, appStatus int, data any) api.ProtocolResult {
	body, _ := json.Marshal(map[string]any{"status": appStatus, "msg": "ok", "data": data})
	return api.ProtocolResult{
		TransportSuccess: true,
		Payload:          api.HTTPResponse{StatusCode: httpStatus, Body: body},
	}
}

func TestRandomCaseIsReferentiallyConsistentAndStrictlyTyped(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 3)
	if item.route["id"] != item.price["routeId"] || item.route["id"] != item.trip["routeId"] {
		t.Fatal("route relationship is inconsistent")
	}
	if item.train["id"] != item.price["trainType"] || item.train["id"] != item.trip["trainTypeId"] {
		t.Fatal("train relationship is inconsistent")
	}
	for service, values := range map[string][]entity{
		"config": {item.config}, "station": {item.stationA, item.stationB},
		"train": {item.train}, "route": {item.route}, "price": {item.price},
		"travel": {item.trip},
	} {
		for _, value := range values {
			normalized, err := normalizedObject(value)
			if err != nil {
				t.Fatal(err)
			}
			if _, err := contracts[service].Validate(normalized, service); err != nil {
				t.Fatal(err)
			}
		}
	}
}

func TestEmbeddedSeedCatalogIsExactAndUnique(t *testing.T) {
	catalog, err := loadSeedCatalog()
	if err != nil {
		t.Fatal(err)
	}
	total := 0
	for service, values := range catalog {
		seen := make(map[string]struct{}, len(values))
		for _, value := range values {
			key, err := contracts[service].Key(value)
			if err != nil {
				t.Fatal(err)
			}
			if _, duplicate := seen[key]; duplicate {
				t.Fatalf("duplicate %s key %q", service, key)
			}
			seen[key] = struct{}{}
			total++
		}
	}
	if total != 45 {
		t.Fatalf("seed entity count=%d, want 45", total)
	}
}

func TestAdminTokenUsesRandomModeNeutralIdentity(t *testing.T) {
	first, err := trainticketsupport.AdminToken(time.Unix(1_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	second, err := trainticketsupport.AdminToken(time.Unix(1_000, 0))
	if err != nil {
		t.Fatal(err)
	}
	if first == second {
		t.Fatal("accuracy identities were reused")
	}
	parts := strings.Split(first, ".")
	if len(parts) != 3 {
		t.Fatalf("token has %d parts", len(parts))
	}
	payload, err := base64.RawURLEncoding.DecodeString(parts[1])
	if err != nil {
		t.Fatal(err)
	}
	var claims map[string]any
	if err := json.Unmarshal(payload, &claims); err != nil {
		t.Fatal(err)
	}
	identity := claims["sub"].(string)
	if claims["id"] != identity || len(identity) != 24 {
		t.Fatalf("claims=%v", claims)
	}
	if strings.Contains(identity, "checker") || strings.Contains(identity, "benchmark") {
		t.Fatalf("mode leaked into identity %q", identity)
	}
}

func TestConfigurationRejectsUnknownFieldsAndNonReusableStationSession(t *testing.T) {
	workload := api.Workload{
		Load:              api.Load{TimeoutSeconds: 1},
		ApplicationConfig: map[string]any{"unknown": int64(1)},
	}
	for _, service := range services {
		workload.Targets = append(workload.Targets, api.Target{
			Name: service, Protocol: "http", SessionPolicy: "reuse",
		})
	}
	if _, err := New(workload); err == nil || !strings.Contains(err.Error(), "unknown") {
		t.Fatalf("unknown field error=%v", err)
	}
	workload.ApplicationConfig = nil
	for index := range workload.Targets {
		if workload.Targets[index].Name == "station" {
			workload.Targets[index].SessionPolicy = "new_per_request"
		}
	}
	if _, err := New(workload); err == nil || !strings.Contains(err.Error(), "session_policy") {
		t.Fatalf("session policy error=%v", err)
	}
}

func TestUpdatesRetainAndProbeOldSecondaryKeys(t *testing.T) {
	catalog, err := loadSeedCatalog()
	if err != nil {
		t.Fatal(err)
	}
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	oldStationNames := []string{stringValue(item.stationA, "name"), stringValue(item.stationB, "name")}
	oldRoute := [2]string{
		stringValue(item.route, "startStationId"), stringValue(item.route, "terminalStationId"),
	}
	oldPrice := [2]string{stringValue(item.price, "routeId"), stringValue(item.price, "trainType")}
	var paths []string
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		paths = append(paths, spec.Path)
		status := 1
		if spec.Method == http.MethodGet {
			status = 0
		}
		return envelopeResult(200, status, nil)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	application := &Application{catalog: catalog, timeout: time.Second}
	if _, err := application.updateCase(
		context.Background(), client, accuracy.NewJournal(), item, rand.New(rand.NewSource(9)),
	); err != nil {
		t.Fatal(err)
	}
	if _, err := verifyRetiredSecondaryIndexes(context.Background(), client, item); err != nil {
		t.Fatal(err)
	}
	for _, oldName := range oldStationNames {
		assertPath(t, paths, servicePath("station", "/stations/id/"+url.PathEscape(oldName)))
	}
	assertPath(t, paths, servicePath("route", "/routes/"+oldRoute[0]+"/"+oldRoute[1]))
	assertPath(t, paths, servicePath("price", "/prices/"+oldPrice[0]+"/"+oldPrice[1]))
}

func TestPartialCreateUsesJournaledReverseCleanup(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	requests := 0
	var deletes []string
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		requests++
		if spec.Method == http.MethodDelete {
			deletes = append(deletes, spec.Path)
			return envelopeResult(200, 1, nil)
		}
		if spec.Method == http.MethodGet && strings.HasSuffix(spec.Path, listPaths[invocation.Target]) {
			return envelopeResult(200, 1, []any{})
		}
		if requests == 3 {
			return api.ProtocolResult{ErrorCategory: "injected", ErrorMessage: "create failed"}
		}
		status := 200
		if spec.Method == http.MethodPost {
			status = 201
		}
		return envelopeResult(status, 1, nil)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	journal := accuracy.NewJournal()
	application := &Application{}
	if _, err := application.createCase(context.Background(), client, journal, item); err == nil {
		t.Fatal("injected create failure was accepted")
	}
	if journal.Active() != 3 {
		t.Fatalf("journal active=%d, want three possibly applied creates", journal.Active())
	}
	if err := journal.Cleanup(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(deletes) != 3 || !strings.Contains(deletes[0], "/stations") || !strings.Contains(deletes[2], "/configs/") {
		t.Fatalf("cleanup order=%v", deletes)
	}
}

func TestCleanupSnapshotsMutablePriceIdentity(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	originalRoute := stringValue(item.price, "routeId")
	deletedRoute := ""
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Method == http.MethodDelete {
			var body map[string]any
			if err := json.Unmarshal([]byte(spec.Body), &body); err != nil {
				t.Fatal(err)
			}
			deletedRoute, _ = body["routeId"].(string)
			return envelopeResult(200, 1, nil)
		}
		return envelopeResult(200, 1, []any{})
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	journal := accuracy.NewJournal()
	if err := recordCleanup(
		journal, item, "price", client, "price", http.MethodDelete, "/prices",
		item.price, item.price,
	); err != nil {
		t.Fatal(err)
	}
	item.price["routeId"] = "mutated-route"
	if err := journal.Cleanup(context.Background()); err != nil {
		t.Fatal(err)
	}
	if deletedRoute != originalRoute {
		t.Fatalf("cleanup route=%q, want snapshotted %q", deletedRoute, originalRoute)
	}
}

func TestCleanupRejectsAcknowledgedNoOpDelete(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Method == http.MethodDelete {
			return envelopeResult(200, 1, nil)
		}
		return envelopeResult(200, 1, []any{item.price})
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	journal := accuracy.NewJournal()
	if err := recordCleanup(
		journal, item, "price", client, "price", http.MethodDelete, "/prices",
		item.price, item.price,
	); err != nil {
		t.Fatal(err)
	}
	if err := journal.Cleanup(context.Background()); err == nil || !strings.Contains(err.Error(), "remains visible") {
		t.Fatalf("no-op cleanup error=%v", err)
	}
	if journal.Active() != 1 {
		t.Fatalf("failed cleanup active=%d, want 1", journal.Active())
	}
}

func TestCleanupDistinguishesOldAndNewPriceCompoundKeys(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	oldPrice := cloneEntity(item.price)
	newPrice := cloneEntity(item.price)
	newPrice["routeId"] = "new-route"
	current := []entity{oldPrice}
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Method == http.MethodDelete {
			var requested entity
			if err := json.Unmarshal([]byte(spec.Body), &requested); err != nil {
				t.Fatal(err)
			}
			status := 0
			for index, price := range current {
				if price["routeId"] == requested["routeId"] && price["trainType"] == requested["trainType"] {
					current = append(current[:index], current[index+1:]...)
					status = 1
					break
				}
			}
			return envelopeResult(200, status, nil)
		}
		items := make([]any, 0, len(current))
		for _, price := range current {
			items = append(items, price)
		}
		return envelopeResult(200, 1, items)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	journal := accuracy.NewJournal()
	if err := recordCleanup(
		journal, item, "old", client, "price", http.MethodDelete, "/prices", oldPrice, oldPrice,
	); err != nil {
		t.Fatal(err)
	}
	if err := recordCleanup(
		journal, item, "new", client, "price", http.MethodDelete, "/prices", newPrice, newPrice,
	); err != nil {
		t.Fatal(err)
	}
	if err := journal.Cleanup(context.Background()); err != nil {
		t.Fatal(err)
	}
	if len(current) != 0 || journal.Active() != 0 {
		t.Fatalf("current=%v active=%d", current, journal.Active())
	}
}

func TestDeleteVerificationRejectsEntityRetainedOnlyInList(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Method == http.MethodGet && spec.Path == servicePath("train", listPaths["train"]) {
			return envelopeResult(200, 1, []any{item.train})
		}
		if spec.Method == http.MethodGet && strings.HasSuffix(spec.Path, listPaths[invocation.Target]) {
			return envelopeResult(200, 1, []any{})
		}
		return envelopeResult(200, 0, nil)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	application := &Application{}
	if _, err := application.verifyDeleted(context.Background(), client, item); err == nil || !strings.Contains(err.Error(), "remains visible") {
		t.Fatalf("retained list entity error=%v", err)
	}
}

func TestDeleteIsolationRejectsMissingSeedRecord(t *testing.T) {
	catalog, err := loadSeedCatalog()
	if err != nil {
		t.Fatal(err)
	}
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		service := invocation.Target
		items := make([]any, 0, len(catalog[service]))
		for index, item := range catalog[service] {
			if service == "config" && index == 0 {
				continue
			}
			items = append(items, item)
		}
		return envelopeResult(200, 1, items)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	application := &Application{catalog: catalog}
	if _, err := application.verifyExactState(context.Background(), client, nil); err == nil || !strings.Contains(err.Error(), "missing key") {
		t.Fatalf("over-delete error=%v", err)
	}
}

func TestExactStateRejectsUnexpectedSchemaValidRecord(t *testing.T) {
	catalog, err := loadSeedCatalog()
	if err != nil {
		t.Fatal(err)
	}
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		service := invocation.Target
		items := make([]any, 0, len(catalog[service])+1)
		for _, item := range catalog[service] {
			items = append(items, item)
		}
		if service == "config" {
			items = append(items, entity{"name": "junk", "value": "valid", "description": "unexpected"})
		}
		return envelopeResult(200, 1, items)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	application := &Application{catalog: catalog}
	if _, err := application.verifyExactState(context.Background(), client, nil); err == nil || !strings.Contains(err.Error(), "unexpected key") {
		t.Fatalf("unexpected-record error=%v", err)
	}
}

func TestRouteSecondaryLookupRejectsUnrelatedRoute(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	unrelated := cloneEntity(item.route)
	unrelated["id"] = "unrelated-route"
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Path == servicePath("route", "/routes/"+stringValue(item.route, "id")) {
			return envelopeResult(200, 1, item.route)
		}
		return envelopeResult(200, 1, []any{item.route, unrelated})
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	if err := verifyRoute(context.Background(), client, item); err == nil || !strings.Contains(err.Error(), "unexpected key") {
		t.Fatalf("unrelated-route error=%v", err)
	}
}

func TestDeleteVerificationRejectsCurrentRouteSecondaryIndex(t *testing.T) {
	item := makeCase(rand.New(rand.NewSource(7)), "namespace", 0)
	stalePath := servicePath(
		"route",
		"/routes/"+stringValue(item.route, "startStationId")+"/"+
			stringValue(item.route, "terminalStationId"),
	)
	runtime := runtimeFunc(func(_ context.Context, invocation api.Invocation) api.ProtocolResult {
		spec := invocation.Payload.(api.HTTPRequestSpec)
		if spec.Path == stalePath {
			return envelopeResult(200, 1, []any{item.route})
		}
		if spec.Method == http.MethodGet && strings.HasSuffix(spec.Path, listPaths[invocation.Target]) {
			return envelopeResult(200, 1, []any{})
		}
		return envelopeResult(200, 0, nil)
	})
	client, err := newClient(runtime, time.Second)
	if err != nil {
		t.Fatal(err)
	}
	application := &Application{}
	if _, err := application.verifyDeleted(context.Background(), client, item); err == nil || !strings.Contains(err.Error(), "application status") {
		t.Fatalf("stale route secondary error=%v", err)
	}
}

func assertPath(t *testing.T, paths []string, want string) {
	t.Helper()
	for _, path := range paths {
		if path == want {
			return
		}
	}
	t.Fatalf("path %q not found in %v", want, paths)
}
