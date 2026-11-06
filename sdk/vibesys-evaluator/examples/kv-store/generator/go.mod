module example.com/kv-store/generator

go 1.25.0

require (
	example.com/kv-store/oracle v0.0.0
	github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go v0.0.0
)

require (
	buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go v1.36.9-20250912141014-52f32327d4b0.1 // indirect
	github.com/BurntSushi/toml v1.5.0 // indirect
	github.com/anishathalye/porcupine v1.3.1 // indirect
	google.golang.org/protobuf v1.36.12 // indirect
	gopkg.in/yaml.v3 v3.0.1 // indirect
)

// A task pins a release instead (github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go
// vX.Y.Z); the example builds against the SDK in this repository.
replace github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go => ../../../go

// The accuracy check's specs are the oracle's (../oracle/kvcheck).
replace example.com/kv-store/oracle => ../oracle
