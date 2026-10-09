"""Read-only AWS tools for Billsense. Demo mode returns hardcoded sample data.

Besides the JSON the model sees, each tool stashes structured data in _state["raw"]
so the UI numbers are built from tool output, never from model text.
"""
import json
import re
import tempfile
import threading
import time
from datetime import date, timedelta
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from strands import tool

COST_ACCESS_CODES = {"AccessDeniedException", "AccessDenied", "UnauthorizedOperation",
                     "OptInRequiredException", "DataUnavailableException"}

CACHE_TTL_SECONDS = 600
SUSPECT_REGIONS = ["ap-south-1", "us-east-1"]

# Estimates only (stated in the UI): NAT GW hourly ~730h + data not included, gp3 us-east-1, EIP idle fee
EST_NAT_MONTHLY = 32.85
EST_EBS_PER_GB = 0.08
EST_EIP_MONTHLY = 3.60

_state = {"demo": False, "used": [], "raw": {}}
_lock = threading.Lock()


# ------------------------------------------------------------------ masking
def mask_id(value: str) -> str:
    value = str(value or "")
    return "••••" + value[-4:] if value else ""


_ARN_RE = re.compile(r"arn:aws[a-z-]*:[^\s,;)\]\"'}]+")
_ACCT_RE = re.compile(r"(?<!\d)\d{12}(?!\d)")
_RES_RE = re.compile(r"\b(?:nat|vol|i|eipalloc|eipassoc|eni|vpc|subnet|igw|sg|snap|ami)-[0-9a-z]{8,17}\b")


def mask_text(text: str) -> str:
    """Hide ARNs, 12-digit account IDs and resource IDs (keep last 4 chars)."""
    text = _ARN_RE.sub("[ARN hidden]", str(text))
    text = _ACCT_RE.sub(lambda m: mask_id(m.group(0)), text)
    return _RES_RE.sub(lambda m: mask_id(m.group(0)), text)


def configure(demo: bool) -> None:
    """Call at the start of each request: sets demo mode and clears per-request state."""
    with _lock:
        _state["demo"] = bool(demo)
        _state["used"] = []
        _state["raw"] = {}
        _state["error"] = None


def error_code():
    """Set when Cost Explorer refused access / is not enabled yet (new accounts)."""
    return _state.get("error")


def tools_used() -> list:
    with _lock:
        return list(_state["used"])


def raw_results() -> dict:
    with _lock:
        return dict(_state["raw"])


def _mark(name: str) -> None:
    with _lock:
        if name not in _state["used"]:
            _state["used"].append(name)


def _dumps(obj) -> str:
    return mask_text(json.dumps(obj, separators=(",", ":"), default=str, ensure_ascii=False))


# ---------------------------------------------------------------- demo data
_DEMO_SERVICES = [  # name, region, previous, current
    ("Amazon Elastic Compute Cloud - Compute", "ap-south-1", 30.00, 30.00),
    ("Amazon Route 53", "global", 5.00, 5.00),
    ("Amazon CloudWatch", "ap-south-1", 4.10, 4.00),
    ("Amazon Virtual Private Cloud (NAT Gateway)", "ap-south-1", 0.00, 3.80),
    ("Amazon Simple Storage Service", "ap-south-1", 2.10, 2.40),
    ("Amazon Elastic Compute Cloud - EBS", "ap-south-1", 0.00, 0.90),
]


def _demo_cost(days: int):
    today = date.today()
    half = max(days // 2, 1)
    movers = [{"service": s, "region": r, "previous": p, "recent": c, "change": round(c - p, 2)}
              for s, r, p, c in _DEMO_SERVICES]
    movers.sort(key=lambda m: abs(m["change"]), reverse=True)
    prev_t = round(sum(m["previous"] for m in movers), 2)
    rec_t = round(sum(m["recent"] for m in movers), 2)
    model = {
        "demo": True, "window_days": days,
        "recent_period": f"{today - timedelta(days=half)} to {today - timedelta(days=1)}",
        "previous_period": f"{today - timedelta(days=2 * half)} to {today - timedelta(days=half + 1)}",
        "latest_data_date": str(today - timedelta(days=1)),
        "total_previous": prev_t, "total_recent": rec_t, "total_change": round(rec_t - prev_t, 2),
        "top_movers": movers,
    }
    # daily series for the current period: flat base + NAT/EBS starting 8 days ago
    base = (30.00 + 2.40 + 4.00 + 5.00) / half
    daily = []
    for i in range(half):
        d = today - timedelta(days=half - i)
        amt = base + ((3.80 + 0.90) / 8 if (today - d).days <= 8 else 0)
        daily.append({"date": d.isoformat(), "amount": round(amt, 2)})
    daily[-1]["amount"] = round(daily[-1]["amount"] + rec_t - sum(d["amount"] for d in daily), 2)  # sum matches total
    services = [{"name": s, "current": c, "previous": p} for s, r, p, c in _DEMO_SERVICES]
    return model, {"services": services, "daily": daily}


def _demo_suspects() -> dict:
    created = (date.today() - timedelta(days=8)).isoformat()
    return {
        "demo": True, "regions_checked": SUSPECT_REGIONS,
        "ap-south-1": {
            "nat_gateways": [{"id": "nat-0demo1234abcd", "created": created, "vpc": "vpc-0demo5678"}],
            "unattached_ebs_volumes": [{"id": "vol-0demo9999", "size_gb": 100, "type": "gp3", "created": created}],
            "unassociated_elastic_ips": [],
            "running_ec2_instances": [{"id": "i-0demo1111", "type": "t3.small"}],
        },
        "us-east-1": {"nat_gateways": [], "unattached_ebs_volumes": [],
                      "unassociated_elastic_ips": [], "running_ec2_instances": []},
    }


# ------------------------------------------------------------------- cache
def _cache_path(days: int) -> Path:
    return Path(tempfile.gettempdir()) / f"billsense_ce2_{days}.json"


def _cache_read(days: int):
    p = _cache_path(days)
    try:
        if p.exists() and time.time() - p.stat().st_mtime < CACHE_TTL_SECONDS:
            data = json.loads(p.read_text(encoding="utf-8"))
            if "model" in data and "extra" in data:
                return data
    except Exception:
        pass
    return None


def _cache_write(days: int, data: dict) -> None:
    try:
        _cache_path(days).write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def _fmt_day(iso: str) -> str:
    d = date.fromisoformat(iso)
    return f"{d.strftime('%b')} {d.day}"


def _summary(model: dict, services: list) -> dict:
    cur, prev = model["total_recent"], model["total_previous"]
    movers = model["top_movers"]
    top = max(movers, key=lambda m: abs(m["change"])) if movers else None
    rec_start, rec_end = [p.strip() for p in model["recent_period"].split(" to ")]
    prev_start, prev_end = [p.strip() for p in model["previous_period"].split(" to ")]
    return {
        "period_label": f"{_fmt_day(rec_start)} – {_fmt_day(rec_end)} vs {_fmt_day(prev_start)} – {_fmt_day(prev_end)}",
        "current_total": cur, "previous_total": prev,
        "change_abs": round(cur - prev, 2),
        "change_pct": round((cur - prev) / prev * 100, 1) if prev else None,
        "top_mover": {"service": top["service"], "region": top["region"], "change_abs": top["change"]} if top else None,
    }


def _store_cost_raw(model: dict, extra: dict) -> None:
    with _lock:
        _state["raw"]["summary"] = _summary(model, extra["services"])
        _state["raw"]["services"] = extra["services"]
        _state["raw"]["daily"] = extra["daily"]


# ------------------------------------------------------------------- tools
@tool
def get_cost_by_service(days: int = 30) -> str:
    """Get AWS spend by service and region, comparing the last days/2 days with the
    previous days/2 days. Returns the top movers (biggest dollar changes) as JSON.

    Args:
        days: Total window in days (default 30). Split into two equal halves.
    """
    _mark("Cost Explorer")
    days = max(2, min(int(days), 90))
    if _state["demo"]:
        model, extra = _demo_cost(days)
        _store_cost_raw(model, extra)
        return _dumps(model)

    cached = _cache_read(days)
    if cached:
        _store_cost_raw(cached["model"], cached["extra"])
        return _dumps(cached["model"])

    half = days // 2
    today = date.today()
    start = today - timedelta(days=2 * half)
    ce = boto3.client("ce", region_name="us-east-1")  # CE only lives in us-east-1
    rows, token = [], None
    try:
        while True:
            kwargs = dict(
                TimePeriod={"Start": start.isoformat(), "End": today.isoformat()},
                Granularity="DAILY", Metrics=["UnblendedCost"],
                GroupBy=[{"Type": "DIMENSION", "Key": "SERVICE"}, {"Type": "DIMENSION", "Key": "REGION"}],
            )
            if token:
                kwargs["NextPageToken"] = token
            resp = ce.get_cost_and_usage(**kwargs)
            rows.extend(resp["ResultsByTime"])
            token = resp.get("NextPageToken")
            if not token:
                break
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        print(f"cost explorer error: {code}")
        if code in COST_ACCESS_CODES:
            _state["error"] = "COST_ACCESS"
            return _dumps({"error": "Cost Explorer is unavailable for this account."})
        raise

    split = (today - timedelta(days=half)).isoformat()
    agg, svc_agg, daily, latest = {}, {}, [], None
    for day in rows:
        d = day["TimePeriod"]["Start"]
        side = "recent" if d >= split else "previous"
        day_total = 0.0
        for g in day["Groups"]:
            svc, reg = g["Keys"]
            amt = float(g["Metrics"]["UnblendedCost"]["Amount"])
            day_total += amt
            agg.setdefault((svc, reg), {"previous": 0.0, "recent": 0.0})[side] += amt
            svc_agg.setdefault(svc, {"previous": 0.0, "recent": 0.0})[side] += amt
        if day_total > 0:
            latest = d
        if side == "recent":
            daily.append({"date": d, "amount": round(day_total, 2)})
    movers = [
        {"service": s, "region": r, "previous": round(v["previous"], 2),
         "recent": round(v["recent"], 2), "change": round(v["recent"] - v["previous"], 2)}
        for (s, r), v in agg.items()
        if abs(v["previous"]) >= 0.005 or abs(v["recent"]) >= 0.005
    ]
    movers.sort(key=lambda m: abs(m["change"]), reverse=True)
    prev_t = round(sum(v["previous"] for v in svc_agg.values()), 2)
    rec_t = round(sum(v["recent"] for v in svc_agg.values()), 2)
    model = {
        "demo": False, "window_days": days,
        "recent_period": f"{split} to {(today - timedelta(days=1)).isoformat()}",
        "previous_period": f"{start.isoformat()} to {(today - timedelta(days=half + 1)).isoformat()}",
        "latest_data_date": latest,
        "total_previous": prev_t, "total_recent": rec_t, "total_change": round(rec_t - prev_t, 2),
        "top_movers": movers[:8],
    }
    if not movers:
        model["note"] = "No cost data returned. The account may be new or Cost Explorer not yet enabled."
    top_services = sorted(svc_agg.items(), key=lambda kv: kv[1]["recent"] + kv[1]["previous"], reverse=True)
    services = [{"name": s, "current": round(v["recent"], 2), "previous": round(v["previous"], 2)}
                for s, v in top_services if abs(v["recent"]) + abs(v["previous"]) >= 0.005][:6]
    extra = {"services": services, "daily": daily}
    _cache_write(days, {"model": model, "extra": extra})
    _store_cost_raw(model, extra)
    return _dumps(model)


def _scan_region(region: str) -> dict:
    ec2 = boto3.client("ec2", region_name=region)
    out = {}

    def paged(method, key, **kw):
        items = []
        for page in ec2.get_paginator(method).paginate(**kw):
            items.extend(page[key])
        return items

    nats = paged("describe_nat_gateways", "NatGateways", Filter=[{"Name": "state", "Values": ["available"]}])
    out["nat_gateways"] = [
        {"id": n["NatGatewayId"], "created": n["CreateTime"].date().isoformat(), "vpc": n.get("VpcId")}
        for n in nats]
    vols = paged("describe_volumes", "Volumes", Filters=[{"Name": "status", "Values": ["available"]}])
    out["unattached_ebs_volumes"] = [
        {"id": v["VolumeId"], "size_gb": v["Size"], "type": v["VolumeType"],
         "created": v["CreateTime"].date().isoformat()} for v in vols]
    addrs = ec2.describe_addresses()["Addresses"]
    out["unassociated_elastic_ips"] = [
        {"ip": a.get("PublicIp"), "allocation_id": a.get("AllocationId")}
        for a in addrs if "AssociationId" not in a]
    insts = []
    for page in ec2.get_paginator("describe_instances").paginate(
            Filters=[{"Name": "instance-state-name", "Values": ["running"]}]):
        for r in page["Reservations"]:
            insts.extend(r["Instances"])
    out["running_ec2_instances"] = [{"id": i["InstanceId"], "type": i["InstanceType"]} for i in insts]
    return out


def _idle_rows(result: dict) -> list:
    rows = []
    for region in SUSPECT_REGIONS:
        r = result.get(region) or {}
        for n in r.get("nat_gateways", []):
            rows.append({"type": "NAT Gateway", "id_masked": mask_id(n["id"]), "region": region,
                         "est_monthly_usd": EST_NAT_MONTHLY})
        for v in r.get("unattached_ebs_volumes", []):
            rows.append({"type": f"Unattached EBS volume ({v['size_gb']} GB)", "id_masked": mask_id(v["id"]),
                         "region": region, "est_monthly_usd": round(v["size_gb"] * EST_EBS_PER_GB, 2)})
        for a in r.get("unassociated_elastic_ips", []):
            rows.append({"type": "Unassociated Elastic IP", "id_masked": mask_id(a.get("allocation_id") or a.get("ip")),
                         "region": region, "est_monthly_usd": EST_EIP_MONTHLY})
    return rows


@tool
def find_usual_suspects() -> str:
    """Look for common sources of idle AWS spend in ap-south-1 and us-east-1: running
    NAT Gateways, unattached EBS volumes, unassociated Elastic IPs and running EC2
    instances. Read-only. Returns JSON."""
    _mark("idle resources")
    if _state["demo"]:
        result = _demo_suspects()
    else:
        result = {"demo": False, "regions_checked": SUSPECT_REGIONS}
        for region in SUSPECT_REGIONS:
            try:
                result[region] = _scan_region(region)
            except Exception as e:  # keep going; report honestly
                print(f"find_usual_suspects failed in {region}: {type(e).__name__}")
                result[region] = {"error": "could not scan this region"}
    with _lock:
        _state["raw"]["idle"] = _idle_rows(result)
    return _dumps(result)
