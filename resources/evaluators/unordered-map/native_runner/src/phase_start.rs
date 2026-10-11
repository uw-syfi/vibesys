use std::sync::{Arc, Condvar, Mutex, MutexGuard};
use std::time::Instant;

#[derive(Default)]
struct PhaseState {
    ready_workers: usize,
    epoch: Option<Instant>,
}

pub struct PhaseStart {
    worker_count: usize,
    state: Mutex<PhaseState>,
    changed: Condvar,
}

pub struct WorkerReady {
    phase: Arc<PhaseStart>,
}

impl PhaseStart {
    pub fn new(worker_count: usize) -> Arc<Self> {
        Arc::new(Self {
            worker_count,
            state: Mutex::new(PhaseState::default()),
            changed: Condvar::new(),
        })
    }

    pub fn worker_ready(self: &Arc<Self>) -> Result<WorkerReady, String> {
        let mut state = self.lock_state()?;
        if state.ready_workers == self.worker_count {
            return Err("too many benchmark workers reported ready".to_string());
        }
        state.ready_workers += 1;
        self.changed.notify_all();
        Ok(WorkerReady {
            phase: Arc::clone(self),
        })
    }

    pub fn publish_when_ready(&self) -> Result<Instant, String> {
        let mut state = self.lock_state()?;
        while state.ready_workers != self.worker_count {
            state = self
                .changed
                .wait(state)
                .map_err(|_| "benchmark phase state lock poisoned".to_string())?;
        }
        drop(state);
        self.try_publish(Instant::now())?
            .ok_or_else(|| "benchmark workers unexpectedly not ready".to_string())
    }

    pub fn try_publish(&self, epoch: Instant) -> Result<Option<Instant>, String> {
        let mut state = self.lock_state()?;
        self.publish(&mut state, epoch)
    }

    fn publish(
        &self,
        state: &mut MutexGuard<'_, PhaseState>,
        epoch: Instant,
    ) -> Result<Option<Instant>, String> {
        if state.epoch.is_some() {
            return Err("benchmark epoch was already published".to_string());
        }
        if state.ready_workers != self.worker_count {
            return Ok(None);
        }
        state.epoch = Some(epoch);
        self.changed.notify_all();
        Ok(Some(epoch))
    }

    fn lock_state(&self) -> Result<MutexGuard<'_, PhaseState>, String> {
        self.state
            .lock()
            .map_err(|_| "benchmark phase state lock poisoned".to_string())
    }
}

impl WorkerReady {
    pub fn wait_for_epoch(self) -> Result<Instant, String> {
        let mut state = self.phase.lock_state()?;
        loop {
            if let Some(epoch) = state.epoch {
                return Ok(epoch);
            }
            state = self
                .phase
                .changed
                .wait(state)
                .map_err(|_| "benchmark phase state lock poisoned".to_string())?;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::PhaseStart;
    use std::time::Instant;

    #[test]
    fn epoch_requires_every_worker_readiness_proof() {
        let phase = PhaseStart::new(2);
        assert_eq!(phase.try_publish(Instant::now()).unwrap(), None);

        let first = phase.worker_ready().unwrap();
        assert_eq!(phase.try_publish(Instant::now()).unwrap(), None);

        let second = phase.worker_ready().unwrap();
        let epoch = Instant::now();
        assert_eq!(phase.try_publish(epoch).unwrap(), Some(epoch));
        assert_eq!(first.wait_for_epoch().unwrap(), epoch);
        assert_eq!(second.wait_for_epoch().unwrap(), epoch);
    }
}
