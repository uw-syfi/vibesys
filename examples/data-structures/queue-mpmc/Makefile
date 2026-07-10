.PHONY: all clean queue-candidate.so

all: queue-candidate.so

queue-candidate.so: Cargo.toml Cargo.lock src/lib.rs
	cargo build --release --locked
	cp "target/release/libqueue_candidate.$$(test "$$(uname -s)" = Darwin && printf dylib || printf so)" "$@"

clean:
	cargo clean
	rm -f queue-candidate.so
