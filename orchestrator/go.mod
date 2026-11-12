module github.com/uw-syfi/vibesys/orchestrator

go 1.26.0

require (
	buf.build/gen/go/bufbuild/protovalidate/protocolbuffers/go v1.36.9-20250912141014-52f32327d4b0.1
	buf.build/go/protovalidate v1.0.0
	github.com/BurntSushi/toml v1.5.0
	golang.org/x/text v0.43.0
	golang.org/x/tools v0.52.0
	google.golang.org/grpc v1.84.0
	google.golang.org/protobuf v1.36.12
	modernc.org/sqlite v1.61.1
)

require (
	github.com/moby/patternmatcher v0.6.0 // indirect
	github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go v0.0.0 // indirect
	golang.org/x/mod v0.42.0 // indirect
	golang.org/x/sync v0.24.0 // indirect
)

require (
	cel.dev/expr v0.25.2 // indirect
	github.com/antlr4-go/antlr/v4 v4.13.1 // indirect
	github.com/dustin/go-humanize v1.1.0 // indirect
	github.com/google/cel-go v0.26.1 // indirect
	github.com/google/uuid v1.6.0 // indirect
	github.com/mattn/go-isatty v0.0.24 // indirect
	github.com/ncruces/go-strftime v1.1.0 // indirect
	github.com/remyoudompheng/bigfft v0.0.0-20230129092748-24d4a6f8daec // indirect
	github.com/stoewer/go-strcase v1.3.1 // indirect
	github.com/uw-syfi/vibesys/cli v0.0.0
	golang.org/x/exp v0.0.0-20250620022241-b7579e27df2b // indirect
	golang.org/x/net v0.61.0 // indirect
	golang.org/x/sys v0.49.0
	google.golang.org/genproto/googleapis/api v0.0.0-20260706201446-f0a921348800 // indirect
	google.golang.org/genproto/googleapis/rpc v0.0.0-20260706201446-f0a921348800
	google.golang.org/grpc/cmd/protoc-gen-go-grpc v1.6.1 // indirect
	modernc.org/libc v1.79.0 // indirect
	modernc.org/mathutil v1.7.1 // indirect
	modernc.org/memory v1.12.1 // indirect
)

tool (
	google.golang.org/grpc/cmd/protoc-gen-go-grpc
	google.golang.org/protobuf/cmd/protoc-gen-go
)

replace github.com/uw-syfi/vibesys/cli => ../cli

replace github.com/uw-syfi/vibesys/sdk/vibesys-evaluator/go => ../sdk/vibesys-evaluator/go
