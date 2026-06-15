# Palo Alto DAG → EDL

`dag_to_edl.py` queries a **Palo Alto NGFW** over the PAN-OS **XML API** for resolved members of one or more **dynamic address groups** (DAGs), then writes a text file suitable for an **IP** [External Dynamic List](https://docs.paloaltonetworks.com/pan-os/11-1/pan-os-admin/policy/use-an-external-dynamic-list-in-policy/formatting-guidelines-for-an-external-dynamic-list/ip-address-list) (EDL).

## Important: NGFW vs Panorama

Live DAG membership is evaluated on the firewall. **Query the NGFW management interface** that returns DAG members (operational `show object dynamic-address-group`). **Panorama** typically does not expose the same resolved membership via this operational path; use the managed device if members are empty or unavailable from Panorama.

## Requirements

- Python 3.10+
- `pip install -r requirements.txt`

## Usage

```text
set PAN_API_KEY=your_xml_api_key

python dag_to_edl.py --host https://192.0.2.1 ^
  --group MyDynamicGroup ^
  --output edl-ip-list.txt
```

### VAR files (default + optional override)

The script always loads **`dag_to_edl.default.var`** from the same directory as `dag_to_edl.py` (if the file is missing, it is treated as empty). You may pass **`--var-file PATH`** to load a second file whose keys **replace** any keys from the default file (shallow merge: same key in the custom file wins).

Format: one **`KEY=value`** per line, UTF-8; blank lines and lines starting with **`#`** are ignored. Duplicate **`GROUP=`** lines in one file define multiple groups.

Supported keys (CLI flags override merged VAR when you pass them explicitly):

| Key | Maps to |
|-----|---------|
| `HOST` | `--host` |
| `API_KEY` | `--api-key` (prefer `PAN_API_KEY` / secrets manager) |
| `VSYS` | `--vsys` |
| `GROUP` | `--group` (repeat, or multiple `GROUP=` lines) |
| `OUTPUT` | `--output` / `-o` |
| `TIMEOUT` | `--timeout` |
| `MAX_ENTRIES` | `--max-entries` |
| `EXPIRED_OUTPUT` | `--expired-output` (empty value = use built-in default path) |
| `INSECURE` | `--insecure` (`true` / `1` / `yes` / `on`) |
| `EXPIRE_DAYS` | `--expire-days` (integer; `0` disables age expiry) |

If you use **`--group`** on the command line, **only** those groups are used for that run (VAR `GROUP` lines are ignored). If you omit **`--group`**, groups must come from **`GROUP=`** in the merged VAR files.

- **`--group`**: repeat for multiple DAGs. Members are merged; if the same indicator appears in more than one group in a single run, the `dag` field in the comment uses the **alphabetically first** group name among those that contained the indicator.
- **`PAN_API_KEY`**: XML API key (or pass `--api-key`).
- **`PAN_VSYS`**: optional; default `vsys1` (same as `--vsys`).
- **`--insecure`**: disable TLS certificate verification (lab only).
- **`--timeout`**: HTTP timeout in seconds (default 60).
- **`--max-entries`**: maximum lines in the main EDL (default 49999); oldest rows are evicted first when exceeded.
- **`--expire-days`**: in addition to the size cap, remove structured lines whose comment **`last=`** (UTC calendar date of the most recent run the indicator appeared in the DAG fetch) is at least **N** full days before today. **`0`** disables. Removed lines go to the same expired archive as capacity evictions. Runs **before** `--max-entries` eviction. Verbatim lines without parseable metadata are not age-expired.
- **`--expired-output`**: path to the unlimited expired-lines archive (default: same directory as `--output`, with `.expired` before the extension, e.g. `edl.txt` → `edl.expired.txt`).

## Size limit and expired archive

The main list is capped (default **49,999** entries). Output order is **oldest first**: structured lines sort by `orig` (then indicator); lines without parseable metadata sort **after** all structured lines.

**Age expiry** (`--expire-days` / `EXPIRE_DAYS`): structured entries whose **`last=`** date is **N** or more UTC calendar days before the run date are removed first and written to the expired archive (same `|rem=` convention as capacity evictions).

When the cap is exceeded, rows are dropped from the **top** of that ordering. Each dropped line is recorded in the expired file with the **same comment text as at removal**, plus **`|rem=YYYY-MM-DD`** (UTC). Verbatim-only lines get ` |rem=YYYY-MM-DD` appended to the preserved line.

The expired archive has **no** entry limit. Whenever evictions occur, the archive is rewritten with all lines sorted by **removal date ascending** (oldest removal first), then by full line text.

The main and archive files use **LF** newlines only.

## Comment schema (dedup ignores comments)

Each line is:

`[indicator] [space] # dag=NAME|orig=YYYY-MM-DD|last=YYYY-MM-DD|cnt=N`

- **indicator**: dedup key (IP, CIDR, or range string as returned by the firewall).
- **dag**: DAG name (deterministic rule for multi-group above).
- **orig**: UTC calendar date when this script **first** recorded this indicator.
- **last**: UTC calendar date of the **most recent run** in which the indicator was still present in the DAG fetch.
- **cnt**: number of runs in which the indicator was **seen in the DAG output**, including the first (`cnt` starts at 1 on first appearance and increments by 1 on each later run where the indicator is still returned).

If an indicator is **no longer** in the DAG fetch, its line is **left unchanged** (stale entries remain until you remove them manually or add a separate pruning tool).

Lines that could not be parsed are **preserved verbatim** when that indicator is absent from the current fetch; if the indicator appears in the fetch, a new structured line is written (re-bootstrap).

Dates use **UTC** (`datetime.now(timezone.utc).date()`).

## Operational command

The script issues:

`show object dynamic-address-group name <name>`

via `type=op` on `/api/`.

## Security

Do not commit API keys or hostnames into git. Prefer environment variables and a secrets manager for automation.
