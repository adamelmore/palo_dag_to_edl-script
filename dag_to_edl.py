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
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Dict, Iterator, List, Mapping, Optional, Set, Tuple
from urllib.parse import urljoin

try:
    import requests
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The 'requests' package is required. Install with: pip install -r requirements.txt"
    ) from exc

# Palo Alto EDL IP list size guidance (stay under platform limits).
MAX_EDL_ENTRIES_DEFAULT = 49999

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
    verify: bool,
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
    verify: bool,
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


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Export PAN-OS dynamic address group members to an IP EDL text file."
    )
    p.add_argument(
        "--host",
        required=True,
        help="NGFW base URL, e.g. https://192.0.2.1",
    )
    p.add_argument(
        "--api-key",
        default=os.environ.get("PAN_API_KEY", ""),
        help="XML API key (default: env PAN_API_KEY)",
    )
    p.add_argument(
        "--vsys",
        default=os.environ.get("PAN_VSYS", "vsys1"),
        help="Virtual system name (default: vsys1 or PAN_VSYS)",
    )
    p.add_argument(
        "--group",
        action="append",
        dest="groups",
        metavar="NAME",
        required=True,
        help="Dynamic address group name (repeat for multiple groups)",
    )
    p.add_argument(
        "--output",
        "-o",
        required=True,
        help="Output EDL file path",
    )
    p.add_argument(
        "--insecure",
        action="store_true",
        help="Disable TLS certificate verification (lab use only)",
    )
    p.add_argument(
        "--timeout",
        type=float,
        default=60.0,
        help="HTTP timeout seconds (default: 60)",
    )
    p.add_argument(
        "--max-entries",
        type=int,
        default=MAX_EDL_ENTRIES_DEFAULT,
        metavar="N",
        help=(
            f"Maximum lines in the main EDL file (default: {MAX_EDL_ENTRIES_DEFAULT}). "
            "Oldest rows are removed first when exceeded."
        ),
    )
    p.add_argument(
        "--expired-output",
        metavar="PATH",
        default=None,
        help=(
            "Unlimited archive for capacity-evicted lines (default: <output> with "
            "'.expired' before the extension, e.g. edl.expired.txt)"
        ),
    )
    return p.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)
    if not args.api_key.strip():
        print("error: missing API key; pass --api-key or set PAN_API_KEY", file=sys.stderr)
        return 2

    groups = list(dict.fromkeys(args.groups))  # de-dupe, preserve order

    lines = load_edl_lines(args.output)
    existing, verbatim, file_warns = parse_edl_file(lines)
    for w in file_warns:
        print(f"warning: {w}", file=sys.stderr)

    verify = not args.insecure
    today = utc_today()

    try:
        member_to_groups = build_fetch_map(
            groups,
            args.host,
            args.api_key.strip(),
            args.vsys,
            verify,
            args.timeout,
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
    ordered_after, expired_new = apply_max_entry_eviction(
        merged_m,
        verbatim_m,
        fetched_keys,
        args.max_entries,
        today,
    )
    expired_path = args.expired_output or default_expired_output_path(args.output)
    if expired_new:
        merge_and_write_expired_archive(expired_path, expired_new)

    write_edl_atomic(
        args.output, merged_m, verbatim_m, fetched_keys, ordered_keys=ordered_after
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
