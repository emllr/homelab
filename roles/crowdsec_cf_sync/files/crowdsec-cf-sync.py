#!/usr/bin/python3
"""Mirror CrowdSec's local ban decisions into Cloudflare account IP Lists.

Managed by homelab (role crowdsec_cf_sync). Runs on hv1 from crowdsec-cf-sync.timer.
Reads each LAPI (web2, web3, ...) with a read-only bouncer key, keeps only bans from our own
detections (origins crowdsec/cscli, never the community list), filters out trusted, private and
Cloudflare ranges, and makes each target account's list match. A WAF custom rule per zone
(`ip.src in $<list>`) does the blocking.

Safety:
  - If any LAPI can't be read, nothing is removed this run (adds only).
  - Only list items whose comment starts with "sync: " are ever removed (manual entries stay).
  - At most max_add new entries per target per run; the list is capped at max_items.
  - dry_run (or a target without a token) only logs what would change.

Config: /etc/crowdsec-cf-sync/config.json. Secrets come from systemd credentials
($CREDENTIALS_DIRECTORY/<name>). Stdlib only.
"""
import ipaddress
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

CF_API = "https://api.cloudflare.com/client/v4"
UA = "crowdsec-cf-sync/1 (homelab)"
COMMENT_PREFIX = "sync: "   # marks entries this job owns (others are never removed)


def log(msg):
    print(msg, flush=True)


def cred(name):
    d = os.environ.get("CREDENTIALS_DIRECTORY", "")
    path = os.path.join(d, name)
    if not d or not os.path.exists(path):
        return None
    with open(path) as f:
        return f.read().strip()


def http(method, url, headers=None, body=None, timeout=20):
    data = None
    h = {"User-Agent": UA, "Accept": "application/json"}
    h.update(headers or {})
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        try:
            errs = json.loads(e.read()).get("errors") or []
            detail = "; ".join(f"{x.get('code')} {x.get('message')}" for x in errs)
        except ValueError:
            detail = ""
        # Path without account/list IDs (they'd end up in Loki).
        where = "/".join(part if len(part) != 32 else "…" for part in urllib.parse.urlsplit(url).path.split("/"))
        if e.code == 429:
            detail += " (rate limited; retried next run)"
        raise RuntimeError(f"{method} {where}: HTTP {e.code} {detail}") from None
    return json.loads(raw) if raw.strip() else None


# ---------- CrowdSec side ----------

def lapi_bans(lapi):
    key = cred(lapi["key_cred"])
    if not key:
        raise RuntimeError(f"no credential {lapi['key_cred']}")
    url = lapi["url"].rstrip("/") + "/v1/decisions?" + urllib.parse.urlencode(
        {"origins": "crowdsec,cscli", "type": "ban"})
    return http("GET", url, {"X-Api-Key": key}) or []


def to_network(value):
    """Decision value -> network in a form Cloudflare lists accept (IPv4 /8-/32, IPv6 /12-/64)."""
    net = ipaddress.ip_network(value, strict=False)
    if net.version == 6 and net.prefixlen > 64:
        net = net.supernet(new_prefix=64)
    if (net.version == 4 and net.prefixlen < 8) or (net.version == 6 and net.prefixlen < 12):
        return None
    return net


def cf_item_value(net):
    if (net.version == 4 and net.prefixlen == 32):
        return str(net.network_address)
    return str(net)


def wanted(cfg, decisions_by_lapi):
    never = [ipaddress.ip_network(c, strict=False) for c in cfg["never"]]
    out, skipped = {}, 0
    for lapi_name, decisions in decisions_by_lapi.items():
        for d in decisions:
            if d.get("type") != "ban" or str(d.get("scope", "")).lower() not in ("ip", "range"):
                continue
            try:
                net = to_network(d["value"])
            except ValueError:
                net = None
            if (net is None or not net.network_address.is_global
                    or any(net.version == n.version and net.overlaps(n) for n in never)):
                skipped += 1
                continue
            v = cf_item_value(net)
            scen = str(d.get("scenario", "")).split("/")[-1][:80]
            out.setdefault(v, f"{COMMENT_PREFIX}{lapi_name} {scen}")
    return out, skipped


# ---------- Cloudflare side ----------

class CF:
    def __init__(self, token, account):
        self.h = {"Authorization": f"Bearer {token}"}
        self.base = f"{CF_API}/accounts/{account}/rules/lists"

    def call(self, method, path="", body=None, query=None):
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        r = http(method, url, self.h, body)
        if not r or not r.get("success"):
            raise RuntimeError(f"Cloudflare {method} {path}: {r and r.get('errors')}")
        return r

    def list_id(self, name, create):
        for lst in self.call("GET")["result"]:
            if lst["name"] == name:
                return lst["id"]
        if not create:
            return None
        r = self.call("POST", body={"name": name, "kind": "ip",
                                    "description": "homelab ban list: local detections, synced from hv1"})
        log(f"created list {name}")
        return r["result"]["id"]

    def items(self, list_id):
        items, cursor = [], None
        while True:
            q = {"per_page": 500}
            if cursor:
                q["cursor"] = cursor
            r = self.call("GET", f"/{list_id}/items", query=q)
            items += r["result"]
            cursor = (r.get("result_info") or {}).get("cursors", {}).get("after")
            if not cursor:
                return items

    def wait(self, op_id):
        for _ in range(30):
            r = self.call("GET", f"/bulk_operations/{op_id}")["result"]
            if r.get("status") == "completed":
                return
            if r.get("status") == "failed":
                raise RuntimeError(f"bulk operation failed: {r.get('error')}")
            time.sleep(1)
        raise RuntimeError("bulk operation still pending after 30 s")

    def add(self, list_id, entries):
        r = self.call("POST", f"/{list_id}/items", body=[{"ip": ip, "comment": c} for ip, c in entries])
        self.wait(r["result"]["operation_id"])

    def remove(self, list_id, ids):
        r = self.call("DELETE", f"/{list_id}/items", body={"items": [{"id": i} for i in ids]})
        self.wait(r["result"]["operation_id"])


def sync_target(cfg, t, want, lapi_failed):
    token = cred(t["token_cred"])
    dry = cfg.get("dry_run", True) or not token
    tag = f"target={t['name']}"
    if not token:
        log(f"{tag}: no token ({t['token_cred']}), dry run: would hold {len(want)} entries")
        return True
    cf = CF(token, t["account_id"])
    list_id = cf.list_id(t["list_name"], create=not dry)
    current = cf.items(list_id) if list_id else []
    have = {i["ip"]: i for i in current}
    to_add = [(ip, c) for ip, c in sorted(want.items()) if ip not in have]
    room = max(0, cfg["max_items"] - len(have))
    capped = to_add[: min(cfg["max_add"], room)]
    to_remove = [] if lapi_failed else [
        i["id"] for ip, i in have.items()
        if ip not in want and str(i.get("comment", "")).startswith(COMMENT_PREFIX)]
    if len(capped) < len(to_add):
        log(f"{tag}: WARNING capped adds {len(to_add)} -> {len(capped)} (max_add/max_items)")
    if dry:
        log(f"{tag}: dry run: list has {len(have)}, would add {len(capped)}, remove {len(to_remove)}"
            + "".join(f"\n  + {ip} ({c})" for ip, c in capped[:20]))
        return True
    if capped:
        cf.add(list_id, capped)
    if to_remove:
        cf.remove(list_id, to_remove)
    log(f"{tag}: list {t['list_name']} now {len(have) + len(capped) - len(to_remove)} "
        f"(added {len(capped)}, removed {len(to_remove)})"
        + "".join(f"\n  + {ip} ({c})" for ip, c in capped[:20]))
    return True


def main():
    with open(sys.argv[1] if len(sys.argv) > 1 else "/etc/crowdsec-cf-sync/config.json") as f:
        cfg = json.load(f)
    ok, decisions, lapi_failed = True, {}, False
    for lapi in cfg["lapis"]:
        try:
            decisions[lapi["name"]] = lapi_bans(lapi)
        except Exception as e:  # keep going with the other LAPIs; no removals this run
            log(f"lapi={lapi['name']}: ERROR {e}")
            ok, lapi_failed = False, True
    want, skipped = wanted(cfg, decisions)
    log(f"decisions: {', '.join(f'{k}={len(v)}' for k, v in decisions.items()) or 'none'}; "
        f"pushable {len(want)}, filtered {skipped}")
    for t in cfg["targets"]:
        try:
            sync_target(cfg, t, want, lapi_failed)
        except Exception as e:
            log(f"target={t['name']}: ERROR {e}")
            ok = False
    if ok:
        log("sync ok" + (" (dry run)" if cfg.get("dry_run", True) else ""))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
