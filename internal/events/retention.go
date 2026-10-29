package events

import (
	"context"
	"database/sql"
	"time"

	"kinich-go/internal/domain"
)

// Prune conserva eventos con entrega recuperable pendiente o dead-letter.
func (s *Service) Prune(ctx context.Context, before time.Time) error {
	return s.Store.Tx(ctx, func(tx *sql.Tx) error {
		rows, err := tx.QueryContext(ctx, "SELECT e.sequence,e.id,e.environment,e.tenant_id FROM events e WHERE json_extract(e.data,'$.created_at')<? AND NOT EXISTS(SELECT 1 FROM deliveries d WHERE d.environment=e.environment AND d.tenant_id=e.tenant_id AND d.event_id=e.id AND d.state!='delivered') ORDER BY e.sequence LIMIT 1000", before.UTC().Format(time.RFC3339Nano))
		if err != nil {
			return err
		}
		type item struct {
			seq int64
			id  string
			o   domain.Owner
		}
		items := []item{}
		for rows.Next() {
			var it item
			if err = rows.Scan(&it.seq, &it.id, &it.o.Environment, &it.o.TenantID); err != nil {
				rows.Close()
				return err
			}
			items = append(items, it)
		}
		err = rows.Err()
		rows.Close()
		if err != nil {
			return err
		}
		for _, it := range items {
			if _, err = tx.ExecContext(ctx, "INSERT INTO retention VALUES(?,?,?) ON CONFLICT(environment,tenant_id) DO UPDATE SET floor=MAX(floor,excluded.floor)", it.o.Environment, it.o.TenantID, it.seq); err != nil {
				return err
			}
			if _, err = tx.ExecContext(ctx, "DELETE FROM delivery_attempts WHERE delivery_id IN (SELECT id FROM deliveries WHERE environment=? AND tenant_id=? AND event_id=?)", it.o.Environment, it.o.TenantID, it.id); err != nil {
				return err
			}
			if _, err = tx.ExecContext(ctx, "DELETE FROM deliveries WHERE environment=? AND tenant_id=? AND event_id=?", it.o.Environment, it.o.TenantID, it.id); err != nil {
				return err
			}
			if _, err = tx.ExecContext(ctx, "DELETE FROM events WHERE sequence=?", it.seq); err != nil {
				return err
			}
		}
		return nil
	})
}
func (s *Service) Retain(ctx context.Context, days int) {
	t := time.NewTicker(time.Hour)
	defer t.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-t.C:
			_ = s.Prune(ctx, time.Now().Add(-time.Duration(days)*24*time.Hour))
		}
	}
}
func (s *Service) CheckCursor(ctx context.Context, o domain.Owner, after int64) error {
	var floor int64
	err := s.Store.DB.QueryRowContext(ctx, "SELECT floor FROM retention WHERE environment=? AND tenant_id=?", o.Environment, o.TenantID).Scan(&floor)
	if err == sql.ErrNoRows {
		return nil
	}
	if err != nil {
		return err
	}
	if after < floor {
		return domain.Error(410, "cursor_expired", "El cursor venció por retención; inicia un replay nuevo")
	}
	return nil
}
