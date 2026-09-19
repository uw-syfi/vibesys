package servicebenchcli

import (
	"context"
	"os"
	"path/filepath"
	"testing"

	"github.com/uw-syfi/vibesys/sdk/vs-evaluator/vseval"

	"vibesys/microservice-evaluator/api"
	"vibesys/microservice-evaluator/composition"
)

type cliAccuracyApplication struct{}

func (cliAccuracyApplication) Name() string { return "custom" }
func (cliAccuracyApplication) Properties() []api.AccuracyProperty {
	return []api.AccuracyProperty{{Name: "required", Required: true}}
}
func (cliAccuracyApplication) ReadinessProbes() []api.ReadinessProbe { return nil }
func (cliAccuracyApplication) PreflightProbes() []api.ReadinessProbe { return nil }
func (cliAccuracyApplication) PreflightProperties() []string         { return []string{"required"} }
func (cliAccuracyApplication) CasePolicy() api.AccuracyCasePolicy {
	return api.AccuracyCasePolicy{MinimumCases: 1}
}
func (cliAccuracyApplication) Check(
	context.Context,
	api.Runtime,
	api.AccuracyContext,
	api.AccuracyRecorder,
) error {
	return nil
}

type cliDriver struct{}

func (cliDriver) Protocol() string { return "custom" }
func (cliDriver) Open(context.Context, api.Target) (api.Client, error) {
	return nil, nil
}

func TestRunRejectsEmptyEngineVersion(t *testing.T) {
	if err := Run(nil, ""); err == nil {
		t.Fatal("Run accepted an empty engine version")
	}
}

func TestRunUsesSuppliedRegistrationsAndFreshFlagSet(t *testing.T) {
	workloadPath := filepath.Join(t.TempDir(), "workload.toml")
	workload := `
version = 1
name = "custom"
application = "custom"

[load]
rate = 1
duration_seconds = 1

[[targets]]
name = "api"
protocol = "custom"
address = "custom://api"

[[operations]]
name = "read"
target = "api"
weight = 1

[objective]
name = "latency"
metric = "latency_ms.p50"
direction = "minimize"
unit = "ms"
`
	if err := os.WriteFile(workloadPath, []byte(workload), 0o600); err != nil {
		t.Fatal(err)
	}
	registrations := []composition.Registration{
		composition.Driver(cliDriver{}),
		composition.AccuracyApplication(
			"custom",
			func(api.Workload) (api.AccuracyApplication, error) {
				return cliAccuracyApplication{}, nil
			},
		),
	}
	args := []string{"--mode", "accuracy", "--workload", workloadPath, "--validate-only"}
	for invocation := 0; invocation < 2; invocation++ {
		if err := Run(args, "test", registrations...); err != nil {
			t.Fatalf("Run() invocation %d: %v", invocation, err)
		}
	}
}

func TestParseCommandJSONRejectsMalformedAndEmptyArguments(t *testing.T) {
	for _, raw := range []string{
		`"./run.sh"`,
		`[]`,
		`["./run.sh",""]`,
		`["./run.sh",1]`,
	} {
		if _, err := parseCommandJSON(raw, "--run-command-json"); err == nil {
			t.Fatalf("accepted invalid command %s", raw)
		}
	}
	command, err := parseCommandJSON(`["./run.sh","--port","8080"]`, "--run-command-json")
	if err != nil {
		t.Fatal(err)
	}
	if len(command) != 3 || command[0] != "./run.sh" {
		t.Fatalf("command=%v", command)
	}
}

func TestParseSeed(t *testing.T) {
	if seed, err := parseSeed("42", "--fixture-seed"); err != nil || seed != 42 {
		t.Fatalf("parseSeed() = %d, %v", seed, err)
	}
	if _, err := parseSeed("not-a-seed", "--fixture-seed"); err == nil {
		t.Fatal("parseSeed accepted a non-integer")
	}
	first, err := parseSeed("random", "--fixture-seed")
	if err != nil || first < 0 {
		t.Fatalf("random seed = %d, %v", first, err)
	}
}

func TestValidateModeFlagsRejectsIgnoredOrMalformedCombinations(t *testing.T) {
	validAccuracy := modeFlagConfig{
		mode: "accuracy", casesMin: 2, casesMax: 5, startupTimeout: 15,
		stateEnv: "VIBESYS_STATE_DIR",
	}
	validBenchmark := modeFlagConfig{
		mode: "benchmark", startupTimeout: 15,
		runCommandJSON: `["./run.sh"]`, stopCommandJSON: `["./stop.sh"]`,
		cleanupCommandJSON: `["./cleanup.sh"]`,
	}
	tests := []struct {
		name   string
		config modeFlagConfig
	}{
		{
			name: "accuracy output raw",
			config: modeFlagConfig{
				mode: "accuracy", explicit: map[string]bool{"output-raw": true},
				outputRaw: "raw.ndjson", casesMin: 2, casesMax: 5, startupTimeout: 15,
				stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "accuracy malformed command",
			config: modeFlagConfig{
				mode: "accuracy", runCommandJSON: `"./run.sh"`,
				casesMin: 2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "accuracy state without command",
			config: modeFlagConfig{
				mode: "accuracy", stateDir: "/tmp/state",
				casesMin: 2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "accuracy stop without command",
			config: modeFlagConfig{
				mode: "accuracy", stopCommandJSON: `["./stop.sh"]`,
				casesMin: 2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "accuracy cleanup without command",
			config: modeFlagConfig{
				mode: "accuracy", cleanupCommandJSON: `["./cleanup.sh"]`,
				casesMin: 2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "benchmark accuracy flag",
			config: modeFlagConfig{
				mode: "benchmark", explicit: map[string]bool{"cases-min": true}, startupTimeout: 15,
			},
		},
		{
			name: "benchmark malformed command",
			config: modeFlagConfig{
				mode: "benchmark", runCommandJSON: `"./run.sh"`, startupTimeout: 15,
			},
		},
		{
			name: "benchmark candidate directory without command",
			config: modeFlagConfig{
				mode: "benchmark", explicit: map[string]bool{"candidate-dir": true},
				startupTimeout: 15,
			},
		},
		{
			name: "accuracy benchmark flag",
			config: modeFlagConfig{
				mode: "accuracy", explicit: map[string]bool{"skip-prepare": true},
				casesMin: 2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "invalid case bounds",
			config: modeFlagConfig{
				mode: "accuracy", casesMin: 5, casesMax: 2, startupTimeout: 15,
				stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "invalid startup timeout",
			config: modeFlagConfig{
				mode: "benchmark", startupTimeout: 0,
			},
		},
		{
			name: "telemetry output without command",
			config: modeFlagConfig{
				mode: "benchmark", startupTimeout: 15,
				telemetryOutput: "telemetry.json", telemetryTimeout: 30,
			},
		},
		{
			name: "telemetry command without output",
			config: modeFlagConfig{
				mode: "benchmark", startupTimeout: 15,
				telemetryCommand: `["./collector"]`, telemetryTimeout: 30,
			},
		},
		{
			name: "malformed telemetry command",
			config: modeFlagConfig{
				mode: "benchmark", startupTimeout: 15,
				telemetryCommand: `"./collector"`, telemetryOutput: "telemetry.json", telemetryTimeout: 30,
			},
		},
		{
			name: "invalid telemetry timeout",
			config: modeFlagConfig{
				mode: "benchmark", startupTimeout: 15,
				telemetryCommand: `["./collector"]`, telemetryOutput: "telemetry.json",
			},
		},
		{
			name: "accuracy telemetry command",
			config: modeFlagConfig{
				mode: "accuracy", explicit: map[string]bool{"telemetry-command-json": true},
				casesMin: 2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
				telemetryCommand: `["./collector"]`, telemetryOutput: "telemetry.json", telemetryTimeout: 30,
			},
		},
		{
			name: "accuracy result stream",
			config: modeFlagConfig{
				mode: "accuracy", explicit: map[string]bool{vseval.OutputFlag: true},
				streamOutput: "stream.jsonl",
				casesMin:     2, casesMax: 5, startupTimeout: 15, stateEnv: "VIBESYS_STATE_DIR",
			},
		},
		{
			name: "result stream without a measurement",
			config: modeFlagConfig{
				mode: "benchmark", explicit: map[string]bool{vseval.OutputFlag: true},
				streamOutput: "stream.jsonl", validateOnly: true, startupTimeout: 15,
			},
		},
		{
			name: "telemetry timeout without command",
			config: modeFlagConfig{
				mode: "benchmark", startupTimeout: 15,
				explicit:         map[string]bool{"telemetry-timeout": true},
				telemetryTimeout: 30,
			},
		},
	}
	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if err := validateModeFlags(test.config); err == nil {
				t.Fatal("invalid flag combination was accepted")
			}
		})
	}
	if err := validateModeFlags(validAccuracy); err != nil {
		t.Fatalf("valid accuracy flags: %v", err)
	}
	if err := validateModeFlags(validBenchmark); err != nil {
		t.Fatalf("valid benchmark flags: %v", err)
	}
	validBenchmark.streamOutput = "stream.jsonl"
	validBenchmark.explicit = map[string]bool{vseval.OutputFlag: true}
	if err := validateModeFlags(validBenchmark); err != nil {
		t.Fatalf("valid benchmark result stream: %v", err)
	}
	validBenchmark.telemetryCommand = `["./collector"]`
	validBenchmark.telemetryOutput = "telemetry.json"
	validBenchmark.telemetryTimeout = 30
	if err := validateModeFlags(validBenchmark); err != nil {
		t.Fatalf("valid benchmark telemetry flags: %v", err)
	}
}

func TestShouldCollectTelemetry(t *testing.T) {
	cases := []struct {
		name    string
		command string
		valid   bool
		want    bool
	}{
		{"no command", "", true, false},
		{"invalid run skips collection", `["./collector"]`, false, false},
		{"valid run with command collects", `["./collector"]`, true, true},
		{"no command invalid run", "", false, false},
	}
	for _, test := range cases {
		t.Run(test.name, func(t *testing.T) {
			if got := shouldCollectTelemetry(test.command, test.valid); got != test.want {
				t.Fatalf("shouldCollectTelemetry(%q, %v) = %v, want %v",
					test.command, test.valid, got, test.want)
			}
		})
	}
}

func TestParseServicebenchCommandRecognizesTraceSubcommand(t *testing.T) {
	trace, args, err := parseServicebenchCommand([]string{"trace", "--workload", "workload.toml"})
	if err != nil || !trace || len(args) != 2 || args[0] != "--workload" {
		t.Fatalf("trace=%v args=%v err=%v", trace, args, err)
	}
	trace, args, err = parseServicebenchCommand([]string{"--workload", "workload.toml"})
	if err != nil || trace || len(args) != 2 {
		t.Fatalf("legacy trace=%v args=%v err=%v", trace, args, err)
	}
	if _, _, err := parseServicebenchCommand([]string{"unknown"}); err == nil {
		t.Fatal("accepted an unknown subcommand")
	}
}

func TestValidateModeFlagsRequiresTraceArtifacts(t *testing.T) {
	base := modeFlagConfig{
		mode: "benchmark", trace: true, startupTimeout: 15,
		telemetryCommand: `["./collector"]`, telemetryOutput: "telemetry.json", telemetryTimeout: 30,
		traceGraphOutput: "trace.json", traceMaxRoots: 10, traceMaxNodes: 30, traceTimelineWidth: 48,
	}
	if err := validateModeFlags(base); err != nil {
		t.Fatalf("valid trace flags: %v", err)
	}
	missing := base
	missing.traceGraphOutput = ""
	if err := validateModeFlags(missing); err == nil {
		t.Fatal("trace mode accepted no graph output")
	}
	accuracy := base
	accuracy.mode = "accuracy"
	if err := validateModeFlags(accuracy); err == nil {
		t.Fatal("trace mode accepted accuracy execution")
	}
}
