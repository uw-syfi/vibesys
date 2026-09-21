use stack_verus_mpmc::MpmcStack;
use std::collections::HashSet;
use std::process::ExitCode;
use std::sync::{Arc, Barrier, Mutex};
use std::thread;

fn sequential_contract() -> Result<(), String> {
    let stack = MpmcStack::new(2);
    if !stack.is_empty() || stack.len() != 0 || stack.pop().is_some() {
        return Err("a new stack must be empty".to_owned());
    }
    if stack.push(10_u64) != Ok(()) || stack.push(20_u64) != Ok(()) {
        return Err("push below capacity must succeed".to_owned());
    }
    if stack.len() != 2 || stack.is_empty() {
        return Err("length must equal the number of stacked values".to_owned());
    }
    if stack.push(30_u64) != Err(30_u64) {
        return Err("push at capacity must return the input value".to_owned());
    }
    if stack.pop() != Some(20_u64) || stack.pop() != Some(10_u64) {
        return Err("pop must preserve LIFO order".to_owned());
    }
    if stack.pop().is_some() || !stack.is_empty() || stack.len() != 0 {
        return Err("draining the stack must restore the empty state".to_owned());
    }
    Ok(())
}

fn producer_order_contract() -> Result<(), String> {
    const PRODUCERS: usize = 4;
    const ITEMS_PER_PRODUCER: usize = 256;
    let stack = Arc::new(MpmcStack::new(PRODUCERS * ITEMS_PER_PRODUCER));
    let start = Arc::new(Barrier::new(PRODUCERS));
    let mut handles = Vec::with_capacity(PRODUCERS);

    for producer in 0..PRODUCERS {
        let stack = Arc::clone(&stack);
        let start = Arc::clone(&start);
        handles.push(thread::spawn(move || {
            start.wait();
            for sequence in 0..ITEMS_PER_PRODUCER {
                let value = ((producer as u64) << 32) | sequence as u64;
                stack
                    .push(value)
                    .map_err(|_| "push unexpectedly reported full".to_owned())?;
            }
            Ok::<(), String>(())
        }));
    }
    for handle in handles {
        handle
            .join()
            .map_err(|_| "producer thread panicked".to_owned())??;
    }

    if stack.len() != PRODUCERS * ITEMS_PER_PRODUCER {
        return Err("concurrent push lost or fabricated an item".to_owned());
    }
    let mut next = [ITEMS_PER_PRODUCER; PRODUCERS];
    for _ in 0..PRODUCERS * ITEMS_PER_PRODUCER {
        let value = stack
            .pop()
            .ok_or_else(|| "stack became empty before every value was returned".to_owned())?;
        let producer = (value >> 32) as usize;
        let sequence = value as u32 as usize;
        if producer >= PRODUCERS || next[producer] == 0 || sequence != next[producer] - 1 {
            return Err("global LIFO order violated a producer's real-time order".to_owned());
        }
        next[producer] -= 1;
    }
    if next != [0; PRODUCERS] || stack.pop().is_some() {
        return Err("concurrent push values were not returned exactly once".to_owned());
    }
    Ok(())
}

fn consumer_conservation_contract() -> Result<(), String> {
    const CONSUMERS: usize = 4;
    const ITEM_COUNT: usize = 1024;
    let stack = Arc::new(MpmcStack::new(ITEM_COUNT));
    for value in 0..ITEM_COUNT as u64 {
        stack
            .push(value)
            .map_err(|_| "prefill unexpectedly reported full".to_owned())?;
    }

    let observed = Arc::new(Mutex::new(Vec::with_capacity(ITEM_COUNT)));
    let start = Arc::new(Barrier::new(CONSUMERS));
    let mut handles = Vec::with_capacity(CONSUMERS);
    for _ in 0..CONSUMERS {
        let stack = Arc::clone(&stack);
        let observed = Arc::clone(&observed);
        let start = Arc::clone(&start);
        handles.push(thread::spawn(move || {
            start.wait();
            let mut local = Vec::with_capacity(ITEM_COUNT / CONSUMERS);
            for _ in 0..ITEM_COUNT / CONSUMERS {
                local.push(
                    stack
                        .pop()
                        .ok_or_else(|| "consumer observed a premature empty stack".to_owned())?,
                );
            }
            observed
                .lock()
                .map_err(|_| "result lock was poisoned".to_owned())?
                .extend(local);
            Ok::<(), String>(())
        }));
    }
    for handle in handles {
        handle
            .join()
            .map_err(|_| "consumer thread panicked".to_owned())??;
    }

    let values = observed
        .lock()
        .map_err(|_| "result lock was poisoned".to_owned())?;
    let unique: HashSet<_> = values.iter().copied().collect();
    if values.len() != ITEM_COUNT
        || unique.len() != ITEM_COUNT
        || unique.iter().any(|value| *value >= ITEM_COUNT as u64)
        || !stack.is_empty()
    {
        return Err("concurrent pop lost, duplicated, or fabricated a value".to_owned());
    }
    Ok(())
}

fn check() -> Result<(), String> {
    sequential_contract()?;
    producer_order_contract()?;
    consumer_conservation_contract()?;
    println!("PASS - pure-Rust exact-linearizable MPMC LIFO checks");
    Ok(())
}

fn main() -> ExitCode {
    match check() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("FAIL - {error}");
            ExitCode::FAILURE
        }
    }
}
