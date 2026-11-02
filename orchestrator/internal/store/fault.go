package store

import (
	"context"
	"database/sql/driver"
	"fmt"
)

// OpKind says which step of a write a fault hook is called at.
type OpKind string

const (
	// OpExec is before a statement that writes. Failing it, or killing the process, leaves the
	// transaction it belongs to uncommitted.
	OpExec OpKind = "exec"
	// OpCommit is before a transaction's COMMIT. A failure rolls the transaction back.
	OpCommit OpKind = "commit"
	// OpCommitted is after a transaction's COMMIT has returned. A failure here leaves the
	// transaction durable and tells the caller it failed, which is what a lost reply looks like.
	OpCommitted OpKind = "committed"
)

// Op is one step of a write to the store's file. The hook in SQLiteOptions sees every one: they are
// taken at the database driver, below the store's own code, so a change to how Commit groups its
// statements into transactions changes the steps a test visits and cannot hide from it.
type Op struct {
	Kind OpKind
	// SQL is the statement of an OpExec, and empty for the others.
	SQL string
}

// faultConnector wraps the connections of the writer's pool so that each write passes the hook.
// It is the extension point modernc.org/sqlite documents (NewConnector), and the wrapper adds
// nothing but the calls to the hook.
type faultConnector struct {
	driver.Connector
	fault func(Op) error
}

func (c faultConnector) Connect(ctx context.Context) (driver.Conn, error) {
	inner, err := c.Connector.Connect(ctx)
	if err != nil {
		return nil, err
	}
	return &faultConn{Conn: inner, fault: c.fault}, nil
}

type faultConn struct {
	driver.Conn
	fault func(Op) error
}

// The driver's connection implements every one of these; the assertions below fail at the first
// use if a driver upgrade ever stops, and the wrapper then needs the matching change.
func (c *faultConn) inner() (driver.ConnBeginTx, driver.ConnPrepareContext, driver.ExecerContext, driver.QueryerContext, error) {
	b, bok := c.Conn.(driver.ConnBeginTx)
	p, pok := c.Conn.(driver.ConnPrepareContext)
	e, eok := c.Conn.(driver.ExecerContext)
	q, qok := c.Conn.(driver.QueryerContext)
	if !bok || !pok || !eok || !qok {
		return nil, nil, nil, nil, fmt.Errorf("store: the SQLite driver's connection lacks a context method the fault hook wraps")
	}
	return b, p, e, q, nil
}

func (c *faultConn) BeginTx(ctx context.Context, opts driver.TxOptions) (driver.Tx, error) {
	b, _, _, _, err := c.inner()
	if err != nil {
		return nil, err
	}
	tx, err := b.BeginTx(ctx, opts)
	if err != nil {
		return nil, err
	}
	return &faultTx{Tx: tx, fault: c.fault}, nil
}

func (c *faultConn) Begin() (driver.Tx, error) {
	return c.BeginTx(context.Background(), driver.TxOptions{})
}

func (c *faultConn) Prepare(query string) (driver.Stmt, error) {
	return c.PrepareContext(context.Background(), query)
}

func (c *faultConn) PrepareContext(ctx context.Context, query string) (driver.Stmt, error) {
	_, p, _, _, err := c.inner()
	if err != nil {
		return nil, err
	}
	st, err := p.PrepareContext(ctx, query)
	if err != nil {
		return nil, err
	}
	return &faultStmt{Stmt: st, query: query, fault: c.fault}, nil
}

func (c *faultConn) ExecContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Result, error) {
	_, _, e, _, err := c.inner()
	if err != nil {
		return nil, err
	}
	if err := c.fault(Op{Kind: OpExec, SQL: query}); err != nil {
		return nil, err
	}
	return e.ExecContext(ctx, query, args)
}

func (c *faultConn) QueryContext(ctx context.Context, query string, args []driver.NamedValue) (driver.Rows, error) {
	_, _, _, q, err := c.inner()
	if err != nil {
		return nil, err
	}
	return q.QueryContext(ctx, query, args)
}

func (c *faultConn) Ping(ctx context.Context) error {
	if p, ok := c.Conn.(driver.Pinger); ok {
		return p.Ping(ctx)
	}
	return nil
}

func (c *faultConn) ResetSession(ctx context.Context) error {
	if r, ok := c.Conn.(driver.SessionResetter); ok {
		return r.ResetSession(ctx)
	}
	return nil
}

func (c *faultConn) IsValid() bool {
	if v, ok := c.Conn.(driver.Validator); ok {
		return v.IsValid()
	}
	return true
}

type faultStmt struct {
	driver.Stmt
	query string
	fault func(Op) error
}

func (s *faultStmt) ExecContext(ctx context.Context, args []driver.NamedValue) (driver.Result, error) {
	if err := s.fault(Op{Kind: OpExec, SQL: s.query}); err != nil {
		return nil, err
	}
	if e, ok := s.Stmt.(driver.StmtExecContext); ok {
		return e.ExecContext(ctx, args)
	}
	values := make([]driver.Value, len(args))
	for i, a := range args {
		values[i] = a.Value
	}
	return s.Stmt.Exec(values) //nolint:staticcheck // The context-free form is the only one a statement without ExecContext has.
}

type faultTx struct {
	driver.Tx
	fault func(Op) error
}

func (t *faultTx) Commit() error {
	if err := t.fault(Op{Kind: OpCommit}); err != nil {
		// The connection must leave the transaction before it goes back to the pool.
		_ = t.Tx.Rollback()
		return err
	}
	if err := t.Tx.Commit(); err != nil {
		return err
	}
	return t.fault(Op{Kind: OpCommitted})
}
