"""Synthetic read provenance for offline fixtures only; never used by runtime."""
import time


def reads(*components):
    now = time.time() * 1000
    return [dict(component=c, started_ms=now, received_ms=now,
                 server_ms=now, elapsed_ms=0.) for c in components]


def timed_snapshot(value):
    components = ['open_orders', 'positions', 'open_orders_end', 'positions_end']
    for cid in value['orders']:
        components.extend(('realtime:' + cid, 'history:' + cid, 'executions:' + cid))
    value['reads'] = reads(*components)
    value['positions'] = [dict(p, mark_price_observed_ms=time.time() * 1000)
                          for p in value['positions']]
    return value
