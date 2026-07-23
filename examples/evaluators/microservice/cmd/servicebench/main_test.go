package main

import "testing"

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
	validBenchmark.telemetryCommand = `["./collector"]`
	validBenchmark.telemetryOutput = "telemetry.json"
	validBenchmark.telemetryTimeout = 30
	if err := validateModeFlags(validBenchmark); err != nil {
		t.Fatalf("valid benchmark telemetry flags: %v", err)
	}
}
