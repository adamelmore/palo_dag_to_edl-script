#!/usr/bin/env python3
"""
Export Palo Alto NGFW dynamic address group (DAG) members to an IP External Dynamic List (EDL) text file.

Uses PAN-OS XML API operational command:
  show object dynamic-address-group name <group>
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Dict, Iterator, List, Mapping, Optional, Set, Tuple, Union
from urllib.parse import urljoin

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Install with: pip install -r requirements.txt"
    ) from exc

# Palo Alto EDL IP list size guidance (stay under platform limits).
MAX_EDL_ENTRIES_DEFAULT = 49999

DEFAULT_VAR_BASENAME = "dag_to_edl.default.var"


def script_directory() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def default_var_file_path() -> str:
    return os.path.join(script_directory(), DEFAULT_VAR_BASENAME)


# ---------------------------------------------------------------------------
# VAR files (KEY=value, UTF-8). Default VAR always loads; --var-file merges
# overrides. CLI arguments override merged VAR values.
# ---------------------------------------------------------------------------


def _parse_bool_var(value: str) -> bool:
    v = value.strip().lower()
    return v in ("1", "true", "yes", "on")


def parse_var_file(path: str) -> Dict[str, Union[str, List[str]]]:
    """
    Load KEY=value lines. Blank lines and lines starting with # are ignored.
    Leading/trailing whitespace on keys and values is stripped.
    Duplicate GROUP= lines append to a list; other duplicate keys replace.
    """
    out: Dict[str, Union[str, List[str]]] = {}
    if not os.path.isfile(path):
        return out
    with open(path, "r", encoding="utf-8", newline="") as f:
        for _lineno, line in enumerate(f, start=1):
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if "=" not in raw:
                continue
            key, val = raw.split("=", 1)
            key_u = key.strip().upper()
            val_s = val.strip()
            if not key_u:
                continue
            if key_u == "GROUP":
                lst = out.get("GROUP")
                if isinstance(lst, list):
                    lst.append(val_s)
                elif isinstance(lst, str):
                    out["GROUP"] = [lst, val_s]
                elif val_s:
                    out["GROUP"] = val_s
                continue
            out[key_u] = val_s
    return out


def merge_var_settings(
    default_path: str,
    custom_path: Optional[str],
) -> Dict[str, Union[str, List[str]]]:
    """Default VAR loads first; custom VAR replaces any keys it defines."""
    merged = dict(parse_var_file(default_path))
    if not custom_path:
        return merged
    merged.update(parse_var_file(custom_path))
    return merged


def _var_groups_to_list(g: Union[str, List[str], None]) -> List[str]:
    if g is None:
        return []
    if isinstance(g, str):
        return [g] if g.strip() else []
    return [x for x in g if str(x).strip()]


def var_settings_to_arg_defaults(
    var: Mapping[str, Union[str, List[str]]],
) -> Dict[str, Any]:
    """Map merged VAR dict to argparse-friendly defaults (typed)."""
    d: Dict[str, Any] = {}
    if "HOST" in var and str(var["HOST"]).strip():
        d["host"] = str(var["HOST"]).strip()
    if "API_KEY" in var and str(var["API_KEY"]).strip():
        d["api_key"] = str(var["API_KEY"]).strip()
    if "VSYS" in var and str(var["VSYS"]).strip():
        d["vsys"] = str(var["VSYS"]).strip()
    if "OUTPUT" in var and str(var["OUTPUT"]).strip():
        d["output"] = str(var["OUTPUT"]).strip()
    if "TIMEOUT" in var and str(var["TIMEOUT"]).strip():
        try:
            d["timeout"] = float(str(var["TIMEOUT"]).strip())
        except ValueError:
            pass
    if "MAX_ENTRIES" in var and str(var["MAX_ENTRIES"]).strip():
        try:
            d["max_entries"] = int(str(var["MAX_ENTRIES"]).strip(), 10)
        except ValueError:
            pass
    if "EXPIRED_OUTPUT" in var:
        eo = str(var["EXPIRED_OUTPUT"]).strip()
        d["expired_output"] = eo if eo else None
    if "INSECURE" in var and str(var["INSECURE"]).strip():
        d["insecure"] = _parse_bool_var(str(var["INSECURE"]))
    if "CA_BUNDLE" in var and str(var["CA_BUNDLE"]).strip():
        d["ca_bundle"] = str(var["CA_BUNDLE"]).strip()
    if "EXPIRE_DAYS" in var and str(var["EXPIRE_DAYS"]).strip():
        try:
            d["expire_days"] = int(str(var["EXPIRE_DAYS"]).strip(), 10)
        except ValueError:
            pass
    if "OUTPUT_BACKUP_COUNT" in var and str(var["OUTPUT_BACKUP_COUNT"]).strip():
        try:
            d["backup_count"] = int(str(var["OUTPUT_BACKUP_COUNT"]).strip(), 10)
        except ValueError:
            pass
    return d


def extract_var_file_argv(argv: List[str]) -> Tuple[Optional[str], List[str]]:
    """Split argv so a pre-parser can read --var-file before applying VAR defaults."""
    var_file: Optional[str] = None
    rest: List[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--var-file":
            if i + 1 >= len(argv):
                raise ValueError("--var-file requires a path")
            var_file = argv[i + 1]
            i += 2
        elif a.startswith("--var-file="):
            var_file = a.split("=", 1)[1]
            i += 1
        else:
            rest.append(a)
            i += 1
    return var_file, rest


def argv_has_flag(argv: List[str], long_opt: str) -> bool:
    """True if argv contains --long-opt or --long-opt=value (or short form -x if provided)."""
    eq = long_opt + "="
    return any(a == long_opt or a.startswith(eq) for a in argv)


# ---------------------------------------------------------------------------
# Comment schema (machine-readable, ASCII)
# Example: # dag=MyDAG|orig=2026-06-10|last=2026-06-15|cnt=4
# ---------------------------------------------------------------------------


@dataclass
class EntryMeta:
    dag: str
    orig: str  # YYYY-MM-DD
    last: str  # YYYY-MM-DD
    cnt: int


def utc_today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def build_comment(meta: EntryMeta) -> str:
    return (
        f"# dag={meta.dag}|orig={meta.orig}|last={meta.last}|cnt={meta.cnt}"
    )


def parse_comment_tail(tail: str) -> Optional[EntryMeta]:
    """
    Parse the portion after the first space on an EDL line (comment).
    Accepts optional leading '#' and whitespace.
    """
    s = tail.strip()
    if s.startswith("#"):
        s = s[1:].strip()
    parts = [p.strip() for p in s.split("|") if p.strip()]
    kv: Dict[str, str] = {}
    for p in parts:
        if "=" not in p:
            continue
        k, v = p.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            kv[k] = v
    if not {"dag", "orig", "last", "cnt"}.issubset(kv.keys()):
        return None
    try:
        cnt = int(kv["cnt"], 10)
    except ValueError:
        return None
    if cnt < 1:
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", kv["orig"]):
        return None
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", kv["last"]):
        return None
    try:
        date.fromisoformat(kv["orig"])
        date.fromisoformat(kv["last"])
    except ValueError:
        return None
    return EntryMeta(dag=kv["dag"], orig=kv["orig"], last=kv["last"], cnt=cnt)


def split_edl_line(line: str) -> Tuple[str, Optional[str]]:
    """
    Split into (indicator, comment_tail). Comment_tail is None if there is no
    separating whitespace after the indicator token.
    """
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return "", None
    m = re.match(r"(\S+)(?:\s+(.*))?$", raw.strip())
    if not m:
        return "", None
    ind, rest = m.group(1), m.group(2)
    if rest is None:
        return ind, None
    return ind, rest


def format_edl_line(indicator: str, meta: EntryMeta) -> str:
    return f"{indicator} {build_comment(meta)}"


def default_expired_output_path(main_output: str) -> str:
    """e.g. edl.txt -> edl.expired.txt"""
    base = os.path.basename(main_output)
    directory = os.path.dirname(os.path.abspath(main_output))
    if "." in base:
        stem, suf = base.rsplit(".", 1)
        name = f"{stem}.expired.{suf}"
    else:
        name = f"{base}.expired"
    return os.path.join(directory, name) if directory else name


def output_keys_ordered(
    meta_by_indicator: Mapping[str, EntryMeta],
    verbatim_by_indicator: Mapping[str, str],
    fetched_indicators: Set[str],
) -> List[str]:
    """
    Main EDL line order: structured entries first, oldest orig first, then
    indicator as tiebreaker; verbatim-only rows (no metadata) last, sorted by
    indicator.
    """
    meta_keys = sorted(
        meta_by_indicator.keys(),
        key=lambda k: (meta_by_indicator[k].orig, k),
    )
    verbatim_only = sorted(
        k
        for k in verbatim_by_indicator
        if k not in fetched_indicators and k not in meta_by_indicator
    )
    return list(meta_keys) + list(verbatim_only)


def edl_indicator_keys(
    meta_by_indicator: Mapping[str, EntryMeta],
    verbatim_by_indicator: Mapping[str, str],
) -> Set[str]:
    return set(meta_by_indicator) | set(verbatim_by_indicator)


@dataclass
class RunSummary:
    dag_fetched: int
    refreshed: int
    added: int
    stale: int
    removed_age: int
    removed_capacity: int
    verbatim: int
    total: int
    max_entries: int
    expired_archived: int
    unchanged: int
    per_group_counts: Dict[str, int]
    added_indicators: List[str]
    removed_age_indicators: List[str]
    removed_capacity_indicators: List[str]
    stale_indicators: List[str]
    multi_group_indicators: List[str]
    cnt_histogram: Dict[int, int]
    stale_age_buckets: Dict[str, int]


def indicators_from_expired_lines(lines: Iterable[str]) -> List[str]:
    out: List[str] = []
    for line in lines:
        ind, _ = split_edl_line(line if line.endswith("\n") else line + "\n")
        if ind:
            out.append(ind)
    return sorted(out)


def per_group_member_counts(
    groups: List[str],
    member_to_groups: Mapping[str, Set[str]],
) -> Dict[str, int]:
    return {
        g: sum(1 for member_groups in member_to_groups.values() if g in member_groups)
        for g in groups
    }


def stale_age_bucket_label(days: int) -> str:
    if days <= 7:
        return "0-7d"
    if days <= 30:
        return "8-30d"
    if days <= 90:
        return "31-90d"
    return "91d+"


def build_stale_age_buckets(
    stale_indicators: Iterable[str],
    final_meta: Mapping[str, EntryMeta],
    final_verbatim: Mapping[str, str],
    today: str,
) -> Dict[str, int]:
    buckets = {"0-7d": 0, "8-30d": 0, "31-90d": 0, "91d+": 0, "verbatim": 0}
    try:
        today_d = date.fromisoformat(today)
    except ValueError:
        return buckets
    for ind in stale_indicators:
        if ind in final_verbatim and ind not in final_meta:
            buckets["verbatim"] += 1
            continue
        meta = final_meta.get(ind)
        if meta is None:
            continue
        try:
            last_d = date.fromisoformat(meta.last)
        except ValueError:
            continue
        buckets[stale_age_bucket_label((today_d - last_d).days)] += 1
    return buckets


def build_run_summary(
    *,
    initial_meta: Mapping[str, EntryMeta],
    initial_verbatim: Mapping[str, str],
    final_meta: Mapping[str, EntryMeta],
    final_verbatim: Mapping[str, str],
    final_ordered_keys: List[str],
    fetched_keys: Set[str],
    member_to_groups: Mapping[str, Set[str]],
    groups: List[str],
    expired_age_lines: List[str],
    expired_capacity_lines: List[str],
    max_entries: int,
    today: str,
) -> RunSummary:
    initial = edl_indicator_keys(initial_meta, initial_verbatim)
    final = set(final_ordered_keys)
    added_indicators = sorted(final - initial)
    stale_indicators = sorted(final - fetched_keys)
    refreshed = len(set(initial_meta.keys()) & fetched_keys & final)
    verbatim = sum(
        1
        for k in final_ordered_keys
        if k in final_verbatim and k not in final_meta
    )
    if max_entries < 1:
        max_entries = MAX_EDL_ENTRIES_DEFAULT
    return RunSummary(
        dag_fetched=len(fetched_keys),
        refreshed=refreshed,
        added=len(added_indicators),
        stale=len(stale_indicators),
        removed_age=len(expired_age_lines),
        removed_capacity=len(expired_capacity_lines),
        verbatim=verbatim,
        total=len(final_ordered_keys),
        max_entries=max_entries,
        expired_archived=len(expired_age_lines) + len(expired_capacity_lines),
        unchanged=len(initial & final),
        per_group_counts=per_group_member_counts(groups, member_to_groups),
        added_indicators=added_indicators,
        removed_age_indicators=indicators_from_expired_lines(expired_age_lines),
        removed_capacity_indicators=indicators_from_expired_lines(
            expired_capacity_lines
        ),
        stale_indicators=stale_indicators,
        multi_group_indicators=sorted(
            m for m, gs in member_to_groups.items() if len(gs) > 1
        ),
        cnt_histogram={
            c: sum(1 for k in final_ordered_keys if k in final_meta and final_meta[k].cnt == c)
            for c in sorted({final_meta[k].cnt for k in final_ordered_keys if k in final_meta})
        },
        stale_age_buckets=build_stale_age_buckets(
            stale_indicators, final_meta, final_verbatim, today
        ),
    )


def _print_indicator_list(label: str, indicators: List[str]) -> None:
    print(f"{label} ({len(indicators)}):", file=sys.stderr)
    for ind in indicators:
        print(f"  {ind}", file=sys.stderr)


def print_run_summary(
    summary: RunSummary,
    *,
    verbose: bool,
    today: str,
    vsys: str,
    output_path: str,
    elapsed_seconds: float,
) -> None:
    removed_total = summary.removed_age + summary.removed_capacity
    headroom = max(summary.max_entries - summary.total, 0)
    print(
        f"dag fetched: {summary.dag_fetched}, refreshed: {summary.refreshed}, "
        f"added: {summary.added}, stale: {summary.stale}, "
        f"removed: {removed_total} (age: {summary.removed_age}, "
        f"capacity: {summary.removed_capacity}), verbatim: {summary.verbatim}, "
        f"total: {summary.total}/{summary.max_entries} (headroom: {headroom}), "
        f"expired archived: {summary.expired_archived}",
        file=sys.stderr,
    )
    if summary.per_group_counts:
        group_parts = ", ".join(
            f"{name}={count}" for name, count in summary.per_group_counts.items()
        )
        print(
            f"groups: {group_parts} (unique merged: {summary.dag_fetched})",
            file=sys.stderr,
        )
    print(
        f"unchanged: {summary.unchanged}, run date: {today}, vsys: {vsys}, "
        f"output: {output_path}, elapsed: {elapsed_seconds:.2f}s",
        file=sys.stderr,
    )
    if not verbose:
        return
    print(
        f"multi-group indicators: {len(summary.multi_group_indicators)}",
        file=sys.stderr,
    )
    if summary.cnt_histogram:
        hist = ", ".join(
            f"cnt={cnt}:{count}"
            for cnt, count in sorted(summary.cnt_histogram.items())
        )
        print(f"cnt histogram: {hist}", file=sys.stderr)
    bucket_parts = ", ".join(
        f"{label}={count}"
        for label, count in summary.stale_age_buckets.items()
        if count
    )
    if bucket_parts:
        print(f"stale age distribution: {bucket_parts}", file=sys.stderr)
    if summary.multi_group_indicators:
        _print_indicator_list("multi-group", summary.multi_group_indicators)
    _print_indicator_list("added", summary.added_indicators)
    _print_indicator_list("removed (age)", summary.removed_age_indicators)
    _print_indicator_list("removed (capacity)", summary.removed_capacity_indicators)
    _print_indicator_list("stale", summary.stale_indicators)


def extract_removal_date(line: str) -> str:
    """Parse |rem=YYYY-MM-DD from an expired-archive line; missing -> sort last."""
    m = re.search(r"\|rem=(\d{4}-\d{2}-\d{2})\s*$", line.rstrip())
    if m:
        return m.group(1)
    return "9999-12-31"


def format_expired_structured_line(indicator: str, meta: EntryMeta, removed: str) -> str:
    """Archive line: same active comment plus removal date (pipe-suffixed)."""
    return f"{indicator} {build_comment(meta)}|rem={removed}"


def format_expired_verbatim_line(verbatim_line: str, removed: str) -> str:
    """Preserve full former line; append removal marker in the comment tail."""
    return f"{verbatim_line.rstrip(chr(10) + chr(13))} |rem={removed}"


def apply_max_entry_eviction(
    meta_by_indicator: Dict[str, EntryMeta],
    verbatim_by_indicator: Dict[str, str],
    fetched_indicators: Set[str],
    max_entries: int,
    removal_date: str,
) -> Tuple[List[str], List[str]]:
    """
    If total output lines would exceed max_entries, drop the oldest lines first
    (start of output_keys_ordered). Returns (ordered_keys_after_eviction,
    new_expired_lines).
    Mutates meta_by_indicator and verbatim_by_indicator in place (copies should
    be passed from caller if originals must be kept).
    """
    ordered = output_keys_ordered(
        meta_by_indicator, verbatim_by_indicator, fetched_indicators
    )
    expired_lines: List[str] = []
    if max_entries < 1:
        max_entries = MAX_EDL_ENTRIES_DEFAULT
    if len(ordered) <= max_entries:
        return ordered, expired_lines

    n_evict = len(ordered) - max_entries
    for k in ordered[:n_evict]:
        if k in meta_by_indicator:
            expired_lines.append(
                format_expired_structured_line(
                    k, meta_by_indicator[k], removal_date
                )
            )
            del meta_by_indicator[k]
            verbatim_by_indicator.pop(k, None)
        elif k in verbatim_by_indicator:
            expired_lines.append(
                format_expired_verbatim_line(
                    verbatim_by_indicator[k], removal_date
                )
            )
            del verbatim_by_indicator[k]

    return ordered[n_evict:], expired_lines


def apply_age_expiration(
    meta_by_indicator: Dict[str, EntryMeta],
    verbatim_by_indicator: Dict[str, str],
    expire_days: int,
    today: str,
    removal_date: str,
) -> List[str]:
    """
    Remove structured entries whose comment |last= UTC calendar date is at
    least expire_days old relative to today (UTC calendar dates), using
    (today - last).days >= expire_days. The |last= field is the most recent
    run date the indicator appeared in the DAG fetch.
    Verbatim-only rows are skipped (no structured last= metadata).
    """
    if expire_days < 1:
        return []
    try:
        today_d = date.fromisoformat(today)
    except ValueError:
        return []
    expired_lines: List[str] = []
    for k in list(meta_by_indicator.keys()):
        meta = meta_by_indicator[k]
        try:
            last_d = date.fromisoformat(meta.last)
        except ValueError:
            continue
        age_days = (today_d - last_d).days
        if age_days < expire_days:
            continue
        expired_lines.append(
            format_expired_structured_line(k, meta, removal_date)
        )
        del meta_by_indicator[k]
        verbatim_by_indicator.pop(k, None)
    return expired_lines


def merge_and_write_expired_archive(
    expired_path: str,
    new_lines: List[str],
) -> None:
    """Append new expired rows, then rewrite entire file sorted by |rem= date."""
    existing: List[str] = []
    if os.path.isfile(expired_path):
        with open(expired_path, "r", encoding="utf-8", newline="") as rf:
            for line in rf:
                s = line.rstrip("\r\n")
                if s.strip():
                    existing.append(s)
    combined = existing + list(new_lines)
    combined.sort(key=lambda ln: (extract_removal_date(ln), ln))

    directory = os.path.dirname(os.path.abspath(expired_path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".dag_to_edl_expired_",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as wf:
            for ln in combined:
                wf.write(ln + "\n")
        os.replace(tmp_path, expired_path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def load_edl_lines(path: str) -> List[str]:
    if not os.path.isfile(path):
        return []
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.readlines()


def parse_edl_file(
    lines: Iterable[str],
) -> Tuple[Dict[str, EntryMeta], Dict[str, str], List[str]]:
    """
    Returns (meta_by_indicator, verbatim_by_indicator, warnings).
    Last line wins if duplicate indicators appear in file.
    Verbatim stores the full original line (without newline) when metadata
    could not be parsed; those lines are preserved on output if the indicator
    is absent from the current DAG fetch.
    """
    warn: List[str] = []
    out: Dict[str, EntryMeta] = {}
    verbatim: Dict[str, str] = {}
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        ind, tail = split_edl_line(line)
        if not ind:
            continue
        raw = line.rstrip("\r\n")
        if tail is None:
            warn.append(
                f"line {lineno}: indicator {ind!r} has no comment metadata; "
                "will re-bootstrap on next successful DAG match"
            )
            verbatim[ind] = raw
            if ind in out:
                del out[ind]
            continue
        meta = parse_comment_tail(tail)
        if meta is None:
            warn.append(
                f"line {lineno}: unparseable metadata for {ind!r}; "
                "will re-bootstrap on next successful DAG match"
            )
            verbatim[ind] = raw
            if ind in out:
                del out[ind]
            continue
        out[ind] = meta
        verbatim.pop(ind, None)
    return out, verbatim, warn


# ---------------------------------------------------------------------------
# PAN-OS XML API
# ---------------------------------------------------------------------------


def build_dynamic_address_group_cmd(group_name: str) -> str:
    show = ET.Element("show")
    obj = ET.SubElement(show, "object")
    dag = ET.SubElement(obj, "dynamic-address-group")
    name_el = ET.SubElement(dag, "name")
    name_el.text = group_name
    return ET.tostring(show, encoding="unicode")


def _local_tag(el: ET.Element, *suffix: str) -> str:
    """Tag without namespace URI."""
    t = el.tag
    if "}" in t:
        t = t.split("}", 1)[1]
    return "/".join((t,) + suffix)


def iter_member_names(result_el: Optional[ET.Element]) -> Iterator[str]:
    if result_el is None:
        return
    for mlist in result_el.iter():
        if _local_tag(mlist) != "member-list":
            continue
        for child in list(mlist):
            if _local_tag(child) != "entry":
                continue
            name = child.get("name")
            if name:
                yield name


def resolve_requests_verify(
    insecure: bool,
    ca_bundle: Optional[str],
) -> Union[bool, str]:
    """Value for ``requests`` *verify*: ``False``, ``True``, or a path to a CA bundle PEM."""
    if insecure:
        import urllib3

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        return False
    path = (ca_bundle or "").strip()
    if path:
        return path
    return True


def parse_dag_op_xml(xml_text: str) -> Tuple[str, List[str]]:
    """
    Returns (status, member_names). status is 'success' or 'error'.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        return "error", [f"invalid XML from firewall: {e}"]

    status = (root.get("status") or "").lower()
    if status != "success":
        msg_el = root.find(".//msg")
        line = (
            (msg_el.text or "").strip()
            if msg_el is not None
            else "unknown API error"
        )
        return "error", [line]

    result = root.find(".//result")
    names = list(iter_member_names(result))
    return "success", names


def fetch_dag_members(
    base_url: str,
    api_key: str,
    group_name: str,
    vsys: str,
    verify: Union[bool, str],
    timeout: float,
) -> List[str]:
    """GET dynamic-address-group members for one group name."""
    cmd = build_dynamic_address_group_cmd(group_name)
    url = urljoin(base_url.rstrip("/") + "/", "api/")
    params: Dict[str, str] = {
        "type": "op",
        "key": api_key,
        "cmd": cmd,
    }
    if vsys and vsys != "shared":
        params["vsys"] = vsys

    resp = requests.get(url, params=params, verify=verify, timeout=timeout)
    resp.raise_for_status()
    status, payload = parse_dag_op_xml(resp.text)
    if status != "success":
        raise RuntimeError(
            f"API error for group {group_name!r}: {payload[0] if payload else 'unknown'}"
        )
    return payload


# ---------------------------------------------------------------------------
# Merge + write
# ---------------------------------------------------------------------------


def build_fetch_map(
    group_names: List[str],
    base_url: str,
    api_key: str,
    vsys: str,
    verify: Union[bool, str],
    timeout: float,
) -> Dict[str, Set[str]]:
    """
    For each member IP/indicator, record which DAG(s) contained it this run.
    """
    member_to_groups: Dict[str, Set[str]] = {}
    for g in group_names:
        members = fetch_dag_members(base_url, api_key, g, vsys, verify, timeout)
        for m in members:
            member_to_groups.setdefault(m, set()).add(g)
    return member_to_groups


def merge_edl(
    existing: Dict[str, EntryMeta],
    member_to_groups: Mapping[str, Set[str]],
    today: str,
) -> Dict[str, EntryMeta]:
    """
    Full output map: all indicators from existing ∪ fetched.
    - In fetch: update or create with cnt rules.
    - Only in existing, not in fetch: unchanged.
    """
    fetched: Set[str] = set(member_to_groups.keys())
    out: Dict[str, EntryMeta] = {}

    for ind, meta in existing.items():
        if ind not in fetched:
            out[ind] = EntryMeta(
                dag=meta.dag, orig=meta.orig, last=meta.last, cnt=meta.cnt
            )

    for ind in sorted(fetched):
        groups = member_to_groups[ind]
        dag = min(groups)
        if ind not in existing:
            out[ind] = EntryMeta(dag=dag, orig=today, last=today, cnt=1)
            continue
        old = existing[ind]
        out[ind] = EntryMeta(
            dag=dag,
            orig=old.orig,
            last=today,
            cnt=old.cnt + 1,
        )

    return out


def write_edl_atomic(
    path: str,
    meta_by_indicator: Mapping[str, EntryMeta],
    verbatim_by_indicator: Mapping[str, str],
    fetched_indicators: Set[str],
    ordered_keys: Optional[List[str]] = None,
) -> None:
    """
    Write merged structured lines plus verbatim lines for indicators that are
    not in the current fetch (per plan: leave unchanged when not in DAG output).
    Lines follow ordered_keys when provided (oldest orig first); otherwise
    default order from output_keys_ordered().
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".dag_to_edl_",
        suffix=".tmp",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as wf:
            keys = ordered_keys
            if keys is None:
                keys = output_keys_ordered(
                    meta_by_indicator, verbatim_by_indicator, fetched_indicators
                )
            for ind in keys:
                if ind in meta_by_indicator:
                    wf.write(format_edl_line(ind, meta_by_indicator[ind]) + "\n")
                elif ind in verbatim_by_indicator:
                    wf.write(verbatim_by_indicator[ind].rstrip("\r\n") + "\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def rotate_numbered_backups(path: str, max_keep: int) -> None:
    """
    Before replacing *path*, retain up to *max_keep* rotated copies ``path.1`` …
    ``path.{max_keep}`` (logrotate-style: current becomes ``.1``, prior ``.1``
    becomes ``.2``, oldest is dropped).

    No-op when *max_keep* <= 0, when *path* does not exist, or when *path* is not
    a regular file.
    """
    if max_keep <= 0:
        return
    ap = os.path.abspath(path)
    if not os.path.isfile(ap):
        return
    oldest = f"{ap}.{max_keep}"
    if os.path.isfile(oldest):
        os.remove(oldest)
    for i in range(max_keep - 1, 0, -1):
        src = f"{ap}.{i}"
        dst = f"{ap}.{i + 1}"
        if os.path.isfile(src):
            os.replace(src, dst)
    os.replace(ap, f"{ap}.1")


def parse_args(
    argv: Optional[List[str]] = None,
) -> Tuple[argparse.Namespace, Dict[str, Union[str, List[str]]]]:
    argv_list = list(sys.argv[1:] if argv is None else argv)
    try:
        var_path_opt, argv_rest = extract_var_file_argv(argv_list)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc

    if var_path_opt and not os.path.isfile(var_path_opt):
        print(f"error: --var-file not found: {var_path_opt}", file=sys.stderr)
        raise SystemExit(2)

    merged_var = merge_var_settings(default_var_file_path(), var_path_opt)
    var_arg_defaults = var_settings_to_arg_defaults(merged_var)

    runtime_defaults: Dict[str, Any] = {
        "api_key": os.environ.get("PAN_API_KEY", ""),
        "vsys": os.environ.get("PAN_VSYS", "vsys1"),
        "timeout": 60.0,
        "max_entries": MAX_EDL_ENTRIES_DEFAULT,
        "expired_output": None,
        "insecure": False,
        "ca_bundle": None,
        "expire_days": 0,
        "host": None,
        "output": None,
        "backup_count": None,
    }
    runtime_defaults.update(var_arg_defaults)
    if runtime_defaults.get("backup_count") is None:
        ev = os.environ.get("PAN_OUTPUT_BACKUP_COUNT", "").strip()
        if ev:
            try:
                runtime_defaults["backup_count"] = int(ev, 10)
            except ValueError:
                runtime_defaults["backup_count"] = 5
        else:
            runtime_defaults["backup_count"] = 5
    if not (runtime_defaults.get("ca_bundle") or "").strip():
        for env_key in (
            "PAN_SSL_CA_BUNDLE",
            "REQUESTS_CA_BUNDLE",
            "SSL_CERT_FILE",
        ):
            ev = os.environ.get(env_key, "").strip()
            if ev:
                runtime_defaults["ca_bundle"] = ev
                break

    p = argparse.ArgumentParser(
        description=(
            "Export PAN-OS dynamic address group members to an IP EDL text file. "
            "The bundled default VAR file is always loaded; use --var-file to override "
            "keys. CLI flags override merged VAR values."
        ),
        epilog=(
            f"VAR files: default is {DEFAULT_VAR_BASENAME} next to this script "
            f"({default_var_file_path()}). Syntax: KEY=value lines; # comments. "
            "Keys: HOST, API_KEY, VSYS, GROUP (repeat per group), OUTPUT, TIMEOUT, "
            "MAX_ENTRIES, EXPIRED_OUTPUT, INSECURE, CA_BUNDLE, EXPIRE_DAYS, "
            "OUTPUT_BACKUP_COUNT."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.set_defaults(**runtime_defaults)

    p.add_argument(
        "--host",
        help="NGFW base URL, e.g. https://192.0.2.1 (VAR: HOST)",
    )
    p.add_argument(
        "--api-key",
        help="XML API key (default: env PAN_API_KEY or VAR: API_KEY)",
    )
    p.add_argument(
        "--vsys",
        help="Virtual system name (VAR: VSYS)",
    )
    p.add_argument(
        "--group",
        action="append",
        dest="groups",
        metavar="NAME",
        help="Dynamic address group name, repeat for multiple (VAR: GROUP lines)",
    )
    p.add_argument(
        "--output",
        "-o",
        metavar="PATH",
        help="Output EDL file path (VAR: OUTPUT)",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (lab use only; VAR: INSECURE)",
    )
    p.add_argument(
        "--ca-bundle",
        metavar="PATH",
        dest="ca_bundle",
        help=(
            "PEM file of CA certificate(s) to trust for HTTPS (private CA). "
            "Ignored when --insecure is set. VAR: CA_BUNDLE. Env (if VAR unset): "
            "PAN_SSL_CA_BUNDLE, REQUESTS_CA_BUNDLE, SSL_CERT_FILE."
        ),
    )
    p.add_argument(
        "--timeout",
        type=float,
        help="HTTP timeout seconds (VAR: TIMEOUT)",
    )
    p.add_argument(
        "--max-entries",
        type=int,
        metavar="N",
        help=(
            f"Maximum lines in the main EDL file (VAR: MAX_ENTRIES; "
            f"code default {MAX_EDL_ENTRIES_DEFAULT}). Oldest rows are removed first "
            "when exceeded."
        ),
    )
    p.add_argument(
        "--expire-days",
        type=int,
        metavar="N",
        dest="expire_days",
        help=(
            "Also remove structured lines whose |last= date in the comment is at "
            "least N UTC calendar days before today (0 disables; VAR: EXPIRE_DAYS). "
            "Runs before --max-entries eviction; removed lines go to the expired archive."
        ),
    )
    p.add_argument(
        "--expired-output",
        metavar="PATH",
        help=(
            "Unlimited archive for evicted lines (VAR: EXPIRED_OUTPUT; default: "
            "<output> with '.expired' before the extension)"
        ),
    )
    p.add_argument(
        "--backup-count",
        type=int,
        metavar="N",
        dest="backup_count",
        help=(
            "If the output file exists, rotate numbered backups path.1 … path.N "
            "before writing (0 disables). Default 5; VAR: OUTPUT_BACKUP_COUNT; "
            "env: PAN_OUTPUT_BACKUP_COUNT (used when VAR omits it)."
        ),
    )
    p.add_argument(
        "--verbose",
        action="store_true",
        help=(
            "Print detailed run metrics: per-indicator lists, multi-group overlap, "
            "cnt histogram, and stale age distribution."
        ),
    )
    args = p.parse_args(argv_rest)

    return args, merged_var


def main(argv: Optional[List[str]] = None) -> int:
    try:
        args, merged_var = parse_args(argv)
    except SystemExit as e:
        code = e.code
        if code in (0, None, False):
            return 0
        if isinstance(code, int):
            return code
        print(str(code), file=sys.stderr)
        return 2

    if not args.api_key or not str(args.api_key).strip():
        print(
            "error: missing API key; pass --api-key, set PAN_API_KEY, or VAR API_KEY",
            file=sys.stderr,
        )
        return 2

    argv_for_flags = list(sys.argv[1:] if argv is None else argv)
    try:
        _, argv_rest = extract_var_file_argv(argv_for_flags)
    except ValueError:
        argv_rest = argv_for_flags

    if argv_has_flag(argv_rest, "--group"):
        groups = list(dict.fromkeys(args.groups or []))
    else:
        groups = list(dict.fromkeys(_var_groups_to_list(merged_var.get("GROUP"))))

    if not args.host or not str(args.host).strip():
        print(
            "error: missing --host (CLI) or HOST= in VAR files",
            file=sys.stderr,
        )
        return 2
    if not args.output or not str(args.output).strip():
        print(
            "error: missing --output (CLI) or OUTPUT= in VAR files",
            file=sys.stderr,
        )
        return 2
    if not groups:
        print(
            "error: no dynamic address groups; pass --group or set GROUP= in VAR files",
            file=sys.stderr,
        )
        return 2

    expire_days = int(args.expire_days or 0)
    if expire_days < 0:
        expire_days = 0

    backup_count = int(args.backup_count)
    if backup_count < 0:
        print(
            "error: --backup-count / OUTPUT_BACKUP_COUNT must be >= 0",
            file=sys.stderr,
        )
        return 2

    lines = load_edl_lines(args.output)
    existing, verbatim, file_warns = parse_edl_file(lines)
    for w in file_warns:
        print(f"warning: {w}", file=sys.stderr)

    ca_path = (getattr(args, "ca_bundle", None) or "").strip()
    if ca_path and not args.insecure and not os.path.isfile(ca_path):
        print(
            f"error: CA bundle is not a readable file: {ca_path}",
            file=sys.stderr,
        )
        return 2

    verify = resolve_requests_verify(bool(args.insecure), ca_path or None)
    today = utc_today()
    run_started = time.perf_counter()

    try:
        member_to_groups = build_fetch_map(
            groups,
            str(args.host).strip(),
            str(args.api_key).strip(),
            str(args.vsys),
            verify,
            float(args.timeout),
        )
    except requests.RequestException as e:
        print(f"error: HTTP request failed: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    merged = merge_edl(existing, member_to_groups, today)
    fetched_keys = set(member_to_groups.keys())

    merged_m = dict(merged)
    verbatim_m = dict(verbatim)
    expired_new: List[str] = []

    expired_age = apply_age_expiration(
        merged_m,
        verbatim_m,
        expire_days,
        today,
        today,
    )
    expired_new.extend(expired_age)

    ordered_after, expired_cap = apply_max_entry_eviction(
        merged_m,
        verbatim_m,
        fetched_keys,
        int(args.max_entries),
        today,
    )
    expired_new.extend(expired_cap)

    max_entries = int(args.max_entries)
    if max_entries < 1:
        max_entries = MAX_EDL_ENTRIES_DEFAULT

    expired_path = args.expired_output or default_expired_output_path(
        str(args.output).strip()
    )
    if expired_new:
        merge_and_write_expired_archive(expired_path, expired_new)

    out_path = str(args.output).strip()
    try:
        rotate_numbered_backups(out_path, backup_count)
    except OSError as e:
        print(f"error: could not rotate output backups: {e}", file=sys.stderr)
        return 1

    write_edl_atomic(
        out_path,
        merged_m,
        verbatim_m,
        fetched_keys,
        ordered_keys=ordered_after,
    )
    summary = build_run_summary(
        initial_meta=existing,
        initial_verbatim=verbatim,
        final_meta=merged_m,
        final_verbatim=verbatim_m,
        final_ordered_keys=ordered_after,
        fetched_keys=fetched_keys,
        member_to_groups=member_to_groups,
        groups=groups,
        expired_age_lines=expired_age,
        expired_capacity_lines=expired_cap,
        max_entries=max_entries,
        today=today,
    )
    print_run_summary(
        summary,
        verbose=bool(args.verbose),
        today=today,
        vsys=str(args.vsys),
        output_path=out_path,
        elapsed_seconds=time.perf_counter() - run_started,
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
