package events

import (
	"bytes"
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"errors"
	"fmt"
	"io"
	"net"
	"net/http"
	"net/netip"
	"net/url"
	"strconv"
	"strings"
	"time"

	"kinich-go/internal/domain"
	"kinich-go/internal/store"
)

type NetworkPolicy struct {
	ApprovedCIDRs []netip.Prefix
	AllowHTTP     bool
}

func (p NetworkPolicy) allowed(ip netip.Addr) bool {
	ip = ip.Unmap()
	for _, cidr := range []string{"0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24", "240.0.0.0/4", "2001:db8::/32"} {
		if netip.MustParsePrefix(cidr).Contains(ip) {
			return false
		}
	}
	if !ip.IsValid() || ip.IsLoopback() || ip.IsLinkLocalUnicast() || ip.IsMulticast() || ip.IsUnspecified() {
		return false
	}
	if ip.IsPrivate() {
		for _, prefix := range p.ApprovedCIDRs {
			if prefix.Contains(ip) {
				return true
			}
		}
		return false
	}
	return ip.IsGlobalUnicast()
}
func (p NetworkPolicy) Validate(raw string) error {
	u, err := url.Parse(raw)
	if err != nil || u.Hostname() == "" || u.User != nil || u.Fragment != "" || (u.Scheme != "https" && !(p.AllowHTTP && u.Scheme == "http")) {
		return domain.Error(422, "callback_url", "Callback requiere HTTPS y destino permitido")
	}
	port := u.Port()
	if port != "" {
		n, err := strconv.Atoi(port)
		if err != nil || n < 1 || n > 65535 {
			return domain.Error(422, "callback_url", "Puerto inválido")
		}
	}
	return nil
}
func (p NetworkPolicy) Dial(ctx context.Context, network, address string) (net.Conn, error) {
	host, port, err := net.SplitHostPort(address)
	if err != nil {
		return nil, err
	}
	ips, err := net.DefaultResolver.LookupNetIP(ctx, "ip", host)
	if err != nil || len(ips) == 0 {
		return nil, errors.New("resolución callback falló")
	}
	for _, ip := range ips {
		if !p.allowed(ip) {
			return nil, errors.New("dirección callback prohibida")
		}
	}
	d := net.Dialer{Timeout: 3 * time.Second}
	return d.DialContext(ctx, network, net.JoinHostPort(ips[0].String(), port))
}
func (p NetworkPolicy) Client() *http.Client {
	return &http.Client{Timeout: 5 * time.Second, CheckRedirect: func(*http.Request, []*http.Request) error { return http.ErrUseLastResponse }, Transport: &http.Transport{Proxy: nil, DialContext: p.Dial, MaxIdleConns: 4, MaxIdleConnsPerHost: 2, ResponseHeaderTimeout: 3 * time.Second, DisableKeepAlives: true, TLSHandshakeTimeout: 3 * time.Second}}
}

type Outbox struct {
	Store  *store.Store
	Policy NetworkPolicy
	Client *http.Client
}

func (o *Outbox) SetDestination(ctx context.Context, d domain.Destination) error {
	if err := o.Policy.Validate(d.URL); err != nil {
		return err
	}
	if err := ValidateCallbackToken(d.Token); err != nil {
		return err
	}
	token, err := o.Store.Seal(d.Owner, d.Token)
	if err != nil {
		return err
	}
	return o.Store.Tx(ctx, func(tx *sql.Tx) error {
		var rev int64
		if err := tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(revision),0)+1 FROM destinations WHERE environment=? AND tenant_id=?", d.Environment, d.TenantID).Scan(&rev); err != nil {
			return err
		}
		_, err := tx.ExecContext(ctx, "INSERT INTO destinations VALUES(?,?,?,?,?)", d.Environment, d.TenantID, rev, d.URL, token)
		return err
	})
}
func (o *Outbox) Run(ctx context.Context) {
	ticker := time.NewTicker(300 * time.Millisecond)
	defer ticker.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case <-ticker.C:
			_ = o.Tick(ctx)
		}
	}
}
func (o *Outbox) Tick(ctx context.Context) error {
	rows, err := o.Store.DB.QueryContext(ctx, "SELECT d.id,d.environment,d.tenant_id,d.event_id,d.destination_revision,d.attempts,e.data,t.url,t.token FROM deliveries d JOIN events e ON e.environment=d.environment AND e.tenant_id=d.tenant_id AND e.id=d.event_id JOIN destinations t ON t.environment=d.environment AND t.tenant_id=d.tenant_id AND t.revision=d.destination_revision WHERE d.state='pending' AND d.next_at<=? ORDER BY d.next_at LIMIT 8", domain.Now())
	if err != nil {
		return err
	}
	type delivery struct {
		id        string
		owner     domain.Owner
		eventID   string
		revision  int64
		attempts  int
		data, url string
		token     []byte
	}
	items := []delivery{}
	for rows.Next() {
		var d delivery
		if err = rows.Scan(&d.id, &d.owner.Environment, &d.owner.TenantID, &d.eventID, &d.revision, &d.attempts, &d.data, &d.url, &d.token); err != nil {
			rows.Close()
			return err
		}
		items = append(items, d)
	}
	err = rows.Err()
	rows.Close()
	if err != nil {
		return err
	}
	for _, d := range items {
		token, err := o.Store.Unseal(d.owner, d.token)
		if err != nil {
			return err
		}
		if err = o.Policy.Validate(d.url); err != nil {
			return err
		}
		req, err := http.NewRequestWithContext(ctx, "POST", d.url, bytes.NewBufferString(d.data))
		if err != nil {
			return err
		}
		req.Header.Set("Content-Type", "application/json")
		req.Header.Set("Authorization", "Bearer "+token)
		req.Header.Set("Idempotency-Key", d.eventID)
		sig := hmac.New(sha256.New, []byte(token))
		sig.Write([]byte(d.data))
		req.Header.Set("X-Kinich-Signature", "sha256="+hex.EncodeToString(sig.Sum(nil)))
		client := o.Client
		if client == nil {
			client = o.Policy.Client()
		}
		res, err := client.Do(req)
		status := 0
		if err == nil {
			status = res.StatusCode
			_, _ = io.Copy(io.Discard, io.LimitReader(res.Body, 4096))
			res.Body.Close()
		}
		attempt := d.attempts + 1
		state := "pending"
		if status >= 200 && status < 300 {
			state = "delivered"
		} else if (status >= 300 && status < 500 && status != 408 && status != 429) || attempt >= 12 {
			state = "dead"
		}
		delay := time.Second * time.Duration(1<<min(attempt, 8))
		err = o.Store.Tx(ctx, func(tx *sql.Tx) error {
			var last int
			if err := tx.QueryRowContext(ctx, "SELECT COALESCE(MAX(attempt),0)+1 FROM delivery_attempts WHERE delivery_id=?", d.id).Scan(&last); err != nil {
				return err
			}
			if _, err := tx.ExecContext(ctx, "INSERT INTO delivery_attempts VALUES(?,?,?,?)", d.id, last, status, domain.Now()); err != nil {
				return err
			}
			_, err := tx.ExecContext(ctx, "UPDATE deliveries SET state=?,attempts=?,next_at=?,last_status=? WHERE id=?", state, attempt, time.Now().Add(delay).UTC().Format(time.RFC3339Nano), status, d.id)
			return err
		})
		if err != nil {
			return err
		}
	}
	return nil
}
func (o *Outbox) List(ctx context.Context, owner domain.Owner) ([]domain.Delivery, error) {
	rows, err := o.Store.DB.QueryContext(ctx, "SELECT id,event_id,state,attempts,next_at,last_status,destination_revision FROM deliveries WHERE environment=? AND tenant_id=? ORDER BY rowid DESC LIMIT 200", owner.Environment, owner.TenantID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := []domain.Delivery{}
	for rows.Next() {
		var d domain.Delivery
		if err = rows.Scan(&d.ID, &d.EventID, &d.State, &d.Attempts, &d.NextAt, &d.LastStatus, &d.DestinationRevision); err != nil {
			return nil, err
		}
		out = append(out, d)
	}
	return out, rows.Err()
}
func ParseCIDRs(s string) ([]netip.Prefix, error) {
	var out []netip.Prefix
	for _, v := range strings.Split(s, ",") {
		if v == "" {
			continue
		}
		p, err := netip.ParsePrefix(v)
		if err != nil {
			return nil, fmt.Errorf("CIDR inválido")
		}
		out = append(out, p)
	}
	return out, nil
}

func ValidateCallbackToken(token string) error {
	if len(token) < 16 || len(token) > 4096 || strings.HasPrefix(token, "kg_") {
		return domain.Error(422, "callback_token", "Usa un token de callback de 16–4096 caracteres, distinto de una llave de control")
	}
	return nil
}
