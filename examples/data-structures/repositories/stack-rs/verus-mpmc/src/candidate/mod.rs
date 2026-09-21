use vstd::atomic::*;
use vstd::prelude::*;
use vstd::resource::ghost_var::GhostVarAuth;
use vstd::resource::Loc;
use vstd::rwlock::{RwLock, RwLockPredicate};

use crate::api::{LenAU, MpmcStack, PopAU, PushAU, StackOperations};
use crate::contract::{LifoToken, StackConstruction};

verus! {

pub(crate) struct State<T> {
    pub(crate) entries: Vec<T>,
    pub(crate) auth: Tracked<GhostVarAuth<Seq<T>>>,
}

pub(crate) struct StackPredicate {
    pub(crate) capacity: usize,
    pub(crate) token_id: Loc,
}

impl<T> RwLockPredicate<State<T>> for StackPredicate {
    closed spec fn inv(self, state: State<T>) -> bool {
        &&& state.auth@.id() == self.token_id
        &&& state.auth@@ == state.entries@
        &&& state.entries@.len() <= self.capacity
    }
}

/// Coarse-lock seed. Candidates may replace this entire representation and all
/// synchronization below while preserving the fixed API contracts.
pub(crate) struct Stack<T> {
    pub(crate) capacity: usize,
    pub(crate) state: RwLock<State<T>, StackPredicate>,
}

impl<T> StackConstruction<T> for Stack<T> {
    open spec fn wf(&self) -> bool {
        &&& self.state.pred().capacity == self.capacity
        &&& self.state.pred().token_id == self.token_id()
    }

    open spec fn token_id(&self) -> Loc {
        self.state.pred().token_id
    }

    open spec fn capacity(&self) -> usize {
        self.capacity
    }

    fn create(capacity: usize) -> (out: (Self, Tracked<LifoToken<T>>)) {
        let tracked (auth, token) = GhostVarAuth::new(Seq::<T>::empty());
        let ghost token_id = token.id();
        let ghost pred = StackPredicate { capacity, token_id };
        let state = State { entries: Vec::new(), auth: Tracked(auth) };
        let stack = Stack { capacity, state: RwLock::new(state, Ghost(pred)) };
        (stack, Tracked(LifoToken { state: token }))
    }
}

impl<T> StackOperations<T> for Stack<T> {
fn push_op(
    stack: &MpmcStack<T>, value: T, Tracked(au): Tracked<PushAU<T>>,
) -> (result: Result<(), T>) {
    proof { stack.expose_model(); }
    let (mut state, handle) = stack.inner.state.acquire_write();
    if state.entries.len() == stack.inner.capacity {
        proof {
            try_open_atomic_update!(au, mut token => {
                state.auth.borrow().agree(&token.state);
                Tracked(Commit((token, Ghost(Err(value)))))
            });
        }
        handle.release_write(state);
        Err(value)
    } else {
        proof {
            assert(state.entries@.len() < stack.inner.capacity);
        }
        state.entries.push(value);
        proof {
            try_open_atomic_update!(au, mut token => {
                state.auth.borrow().agree(&token.state);
                state.auth.borrow_mut().update(&mut token.state, state.entries@);
                Tracked(Commit((token, Ghost(Ok(())))))
            });
        }
        proof {
            assert(state.entries@.len() <= stack.inner.capacity);
        }
        handle.release_write(state);
        Ok(())
    }
}

fn pop_op(
    stack: &MpmcStack<T>, Tracked(au): Tracked<PopAU<T>>,
) -> (result: Option<T>) {
    proof { stack.expose_model(); }
    let (mut state, handle) = stack.inner.state.acquire_write();
    let result = state.entries.pop();
    proof {
        try_open_atomic_update!(au, mut token => {
            state.auth.borrow().agree(&token.state);
            state.auth.borrow_mut().update(&mut token.state, state.entries@);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_write(state);
    result
}

fn len_op(
    stack: &MpmcStack<T>, Tracked(au): Tracked<LenAU<T>>,
) -> (result: usize) {
    proof { stack.expose_model(); }
    let handle = stack.inner.state.acquire_read();
    let state = handle.borrow();
    let result = state.entries.len();
    proof {
        try_open_atomic_update!(au, token => {
            state.auth.borrow().agree(&token.state);
            Tracked(Commit((token, Ghost(result))))
        });
    }
    handle.release_read();
    result
}
}

} // verus!
