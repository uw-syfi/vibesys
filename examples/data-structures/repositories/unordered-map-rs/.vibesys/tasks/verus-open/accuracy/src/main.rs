use std::collections::HashMap;
use std::process::ExitCode;
use std::sync::{Arc, Barrier};
use std::thread;
use unordered_map_verus::ConcurrentMap;

fn sequential_contract() -> Result<(), String> {
    let map = ConcurrentMap::new();
    if map.len() != 0 || map.get(1).is_some() || map.remove(1).is_some() {
        return Err("a new map must be empty".to_owned());
    }
    if map.put(1, 10) != None || map.put(2, 20) != None {
        return Err("put of a missing key must return None".to_owned());
    }
    if map.get(1) != Some(10) || map.get(2) != Some(20) || map.len() != 2 {
        return Err("get must return the last put of that key".to_owned());
    }
    if map.put(1, 11) != Some(10) || map.get(1) != Some(11) {
        return Err("put of a present key must replace and return the old value".to_owned());
    }
    if map.remove(2) != Some(20) || map.get(2).is_some() || map.len() != 1 {
        return Err("remove must return the old value and delete the key".to_owned());
    }
    if map.remove(2).is_some() {
        return Err("remove of a missing key must return None".to_owned());
    }
    Ok(())
}

fn concurrent_put_get_contract() -> Result<(), String> {
    const WRITERS: usize = 4;
    const ITEMS: usize = 256;
    let map = Arc::new(ConcurrentMap::new());
    let start = Arc::new(Barrier::new(WRITERS));
    let mut handles = Vec::with_capacity(WRITERS);
    for writer in 0..WRITERS {
        let map = Arc::clone(&map);
        let start = Arc::clone(&start);
        handles.push(thread::spawn(move || {
            start.wait();
            for sequence in 0..ITEMS {
                let key = ((writer as u64) << 32) | sequence as u64;
                if map.put(key, key + 1).is_some() {
                    return Err("disjoint put observed a pre-existing key".to_owned());
                }
            }
            Ok::<(), String>(())
        }));
    }
    for handle in handles {
        handle
            .join()
            .map_err(|_| "writer thread panicked".to_owned())??;
    }
    if map.len() != WRITERS * ITEMS {
        return Err("concurrent put lost or fabricated a key".to_owned());
    }
    for writer in 0..WRITERS {
        for sequence in 0..ITEMS {
            let key = ((writer as u64) << 32) | sequence as u64;
            if map.get(key) != Some(key + 1) {
                return Err("get missed a concurrently inserted mapping".to_owned());
            }
        }
    }
    Ok(())
}

fn concurrent_remove_contract() -> Result<(), String> {
    const KEYS: usize = 1024;
    let map = Arc::new(ConcurrentMap::new());
    for key in 0..KEYS as u64 {
        map.put(key, key + 7);
    }
    let start = Arc::new(Barrier::new(4));
    let mut handles = Vec::with_capacity(4);
    for worker in 0..4 {
        let map = Arc::clone(&map);
        let start = Arc::clone(&start);
        handles.push(thread::spawn(move || {
            start.wait();
            let mut removed = HashMap::new();
            let mut key = worker as u64;
            while key < KEYS as u64 {
                if let Some(value) = map.remove(key) {
                    removed.insert(key, value);
                }
                key += 4;
            }
            Ok::<_, String>(removed)
        }));
    }
    let mut combined = HashMap::new();
    for handle in handles {
        let local = handle
            .join()
            .map_err(|_| "remover thread panicked".to_owned())??;
        for (key, value) in local {
            if combined.insert(key, value).is_some() {
                return Err("concurrent remove duplicated a key".to_owned());
            }
        }
    }
    if combined.len() != KEYS || map.len() != 0 {
        return Err("concurrent remove lost a key or left residue".to_owned());
    }
    for key in 0..KEYS as u64 {
        if combined.get(&key) != Some(&(key + 7)) {
            return Err("remove returned the wrong value".to_owned());
        }
    }
    Ok(())
}

fn check() -> Result<(), String> {
    sequential_contract()?;
    concurrent_put_get_contract()?;
    concurrent_remove_contract()?;
    println!("PASS - pure-Rust exact-linearizable unordered map checks");
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
