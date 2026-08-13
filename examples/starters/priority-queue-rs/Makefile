.PHONY: all clean priority-queue-candidate.so

all: priority-queue-candidate.so

priority-queue-candidate.so: Cargo.toml Cargo.lock src/lib.rs src/ffi.rs
	cargo build --release --locked
	cp "target/release/libpriority_queue_candidate.$$(test "$$(uname -s)" = Darwin && printf dylib || printf so)" "$@"

clean:
	cargo clean
	rm -f priority-queue-candidate.so
