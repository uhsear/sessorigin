# sessorigin

Give a verdict from exported session rows after a database alias flip. Never report MIGRATED
while a node still holds sessions on the old server.

A geodatabase moves to a new SQL Server. Every ArcGIS Server service connects through a DNS
alias, so the plan is simple: repoint the alias, and the services follow. A rehearsal tests the
plan on one node. A script watches the node's sessions on both servers, and it prints
`MIGRATED` the moment one session appears on the new server.

That word was wrong. The warm connection pool never drained by itself. A per-service recycle
moved most of the connections and left a few on the old server. For several minutes the node
answered identical requests from both databases. Every response was HTTP 200, and no log had an
entry. At cutover the two databases hold the same rows, so a feature count cannot see the
difference. Edits that reach the old database are lost when it is retired.

The check that sees it is on the server side: which server holds the node's sessions. This tool
reads those sessions, polled from both servers round by round, and gives one verdict per node.
`MIGRATED` needs zero sessions on the old server in every round of a settle window. A node with
sessions on both servers is `SPLIT`, whatever the new server shows.

```
$ python sessorigin.py --self-test
sessorigin self-test: no database, no network, no credentials
--------------------------------------------------------------------
PASS  sessions on NEW while 2 remain on OLD is SPLIT, never MIGRATED  <-- pinned defect
PASS  and the reason names both counts and the round
PASS  and SPLIT fails the gate with exit 1
PASS  the split rounds are 3, 4 and 5
PASS  the first round with a session on NEW is 3
PASS  one survivor on OLD beside seven on NEW is still SPLIT  <-- pinned defect
...
PASS  a node with no session anywhere at the end is GONE, not MIGRATED  <-- pinned defect
...
PASS  no session on OLD at baseline is INCONCLUSIVE, not MIGRATED  <-- pinned defect
PASS  a node already on NEW at baseline is INCONCLUSIVE  <-- pinned defect
PASS  a baseline round with no poll of NEW is INCONCLUSIVE
PASS  a poll of OLD without VIEW SERVER STATE is blind, not a zero  <-- pinned defect
PASS  a final round with no poll of OLD is INCONCLUSIVE, not MIGRATED  <-- pinned defect
...
PASS  an export with no row for OLD is refused and names the alias trap  <-- pinned defect
...
PASS  a hosts line on disk while the resolver answers the old address FAILS  <-- pinned defect
...
PASS  a prefix --ap of --apply is refused by the parser  <-- pinned defect
PASS  a prefix --appl of --apply is refused by the parser  <-- pinned defect
...
PASS  --node app-01 beside a split app-02 is not MIGRATED and exits 3  <-- pinned defect
PASS  an ASCII-only console gets the report, not a traceback and exit 1  <-- pinned defect
PASS  --out naming the export is refused and the export is untouched  <-- pinned defect
...
PASS  a failed preflight withholds every session verdict  <-- pinned defect
PASS  and a withheld run writes nothing, even with --apply  <-- pinned defect
...
PASS  loading the file as a module runs nothing and prints nothing
PASS  a run with a failure says so and returns 1
--------------------------------------------------------------------
139 assertions, 0 failed
```

The full run prints all 139 assertions. The `...` lines show where this block is cut.

## Requirements

Python 3.9 or newer, and nothing else. No database driver, no third-party package, no network.
The tool reads a CSV that you export from both servers.

The self-test prints 139 assertions, 0 failed, on each of these interpreters: Windows Python
3.13.2, Windows Python 3.9.25, and Ubuntu Python 3.12.3. The outputs are identical line for line.
Branch coverage of the self-test is 100 percent (814 statements, 210 branches, measured with
coverage.py 7.16.1 on Windows). The self-test starts one subprocess, to prove that a console
which cannot print non-ASCII text still gets the report. Coverage does not trace that
subprocess, and the same lines run in the test process.

```
git clone https://github.com/uhsear/sessorigin.git
python sessorigin.py --self-test
```

## Usage

```
python sessorigin.py polls.csv --old DBOLD --new DBNEW
python sessorigin.py polls.csv --old DBOLD --new DBNEW --node app-01 --node app-02
python sessorigin.py polls.csv --old DBOLD --new DBNEW --settle 3
python sessorigin.py --alias db.example.test --expect 198.51.100.20 --resolved 192.0.2.10 --hosts-file hosts.txt
python sessorigin.py polls.csv --old DBOLD --new DBNEW --node app-01 --alias db.example.test --expect 198.51.100.20 --resolved 198.51.100.20
python sessorigin.py polls.csv --old DBOLD --new DBNEW --out verdicts.csv --apply
```

| Flag | Default | What it does |
|---|---|---|
| `SESSIONS` | none | The exported poll rows, as a CSV. |
| `--old` | none | `@@SERVERNAME` of the server the alias left. Required with `SESSIONS`. |
| `--new` | none | `@@SERVERNAME` of the server the alias now names. Required with `SESSIONS`. |
| `--node` | every host | A client `host_name` to judge. Repeat it for more nodes. |
| `--settle` | `2` | The number of last rounds that must all show the node only on NEW. |
| `--alias` | none | Preflight: the DNS name that was flipped. |
| `--expect` | none | Preflight: the address the alias must resolve to. |
| `--resolved` | none | Preflight: an address the node's resolver returned. Repeat it for each address. |
| `--hosts-file` | none | Preflight: a copy of the node's hosts file. Optional. |
| `--out` | off | Write one row per node to this CSV. Needs `--apply`. |
| `--apply` | off | Write `--out`. Without it, no file is written. |
| `--self-test` | off | Run the assertions and exit. It writes fixture files to a temporary directory and removes the directory. |

`--node` limits the verdicts, not the check. The tool still judges every other host in the
export. If one of them is `SPLIT`, `STAYED` or `UNSETTLED`, the run prints it under "not clean",
says `NOT CERTIFIED`, and exits 3 instead of 0. A run that leaves nothing out ends with "Every
node named by --node". A run without `--node` ends with "Every node", which means every host in
the export.

The parser sets `allow_abbrev=False`. A prefix such as `--ap` is refused, and it is never read
as `--apply`. The self-test pins this for `--ap` and `--appl`.

## The export

Poll both servers once before the flip, then once a minute after it. Each poll of each server
is one round of rows. Poll each server by its own name, never through the alias. After the
flip, the alias answers as the new server, and `@@SERVERNAME` reports the server that answered.

This query gives the five required columns and the optional `poll_time`. It was written against
the Microsoft documentation cited below. **It has not been run against a SQL Server by this
repository's tests.** The tool's tests start from the CSV.

```sql
-- One round, one server. $(round) is a scripting variable that
-- Invoke-Sqlcmd sets with -Variable "round=3".
SELECT $(round)                                            AS round,
       @@SERVERNAME                                        AS server,
       s.host_name,
       s.session_id,
       HAS_PERMS_BY_NAME(NULL, NULL, 'VIEW SERVER STATE')  AS view_server_state,
       SYSUTCDATETIME()                                    AS poll_time
FROM (SELECT 1 AS one) AS p
LEFT JOIN sys.dm_exec_sessions AS s
       ON s.is_user_process = 1
      AND s.session_id <> @@SPID
      AND s.host_name IN ('app-01', 'app-02');
```

Run the query with `Invoke-Sqlcmd` and pipe the rows to `Export-Csv`. Run it from a machine whose
`host_name` is not in the `IN` list. The `session_id <> @@SPID` line leaves out the poll's own
session. A poller whose `host_name` is in the list would count as a node on both servers, and
every run would stop at the baseline with `INCONCLUSIVE`.

| Column | Required | What the tool does with it |
|---|---|---|
| `round` | yes | A whole number. Rounds sort as numbers. The lowest round is the baseline. |
| `server` | yes | Must match `--old` or `--new`, without case. Any other name is refused. |
| `host_name` | yes | The node. Compared without case. |
| `session_id` | yes | Counted once per server per round. A blank value marks a poll that matched no session. |
| `view_server_state` | yes | Must be `1`. Any other value makes that poll blind. |
| `poll_time` | no | Printed as it is. It is never parsed. |

The `LEFT JOIN` from a one-row table gives one row with a blank `session_id` when no session
matches. That row proves that the server was polled. Without it, "no rows" means both "zero
sessions" and "the poll failed".

Column names match without case and without surrounding blanks. Windows PowerShell 5.1
`Export-Csv` writes a `#TYPE` line above the header unless you give `-NoTypeInformation`. The
tool skips that line. `Export-Csv` converts each value with `ToString()`, and `DBNull.ToString()`
returns an empty string, so a SQL `NULL` arrives as a blank cell. A UTF-8 BOM is stripped. A
UTF-16 file is refused with exit 2, because reading it as UTF-8 gives no usable column. A column
that the tool does not know is ignored.

Only `Invoke-Sqlcmd` with `Export-Csv` is supported. The `sqlcmd` program prints the text `NULL`
for a `NULL` cell and a row of dashes under the header. The tool then reads a node called `NULL`,
or a server called `------`. Both fail closed: the first exits 3 and the second exits 64. If you
use `sqlcmd`, remove the dashes row, add the header yourself, and blank the `NULL` cells.

## What a blind poll is

Without `VIEW SERVER STATE`, `sys.dm_exec_sessions` shows the caller only its own session. On
SQL Server 2022 and later, the view needs `VIEW SERVER PERFORMANCE STATE`. A poll without the
permission sees zero sessions from every node, which looks exactly like a migration. The tool
calls that poll blind. A blind poll in the baseline round or in the settle window makes the node
`INCONCLUSIVE`. If you granted `VIEW SERVER PERFORMANCE STATE` and not `VIEW SERVER STATE`,
change the permission name in `HAS_PERMS_BY_NAME`.

## Verdicts

| Verdict | Condition, at the last round | Exit |
|---|---|---|
| `MIGRATED` | 0 on OLD and at least 1 on NEW, in each of the last `--settle` rounds. The reason quotes the lowest NEW count in the window. | 0 |
| `SPLIT` | At least 1 on OLD and at least 1 on NEW. | 1 |
| `STAYED` | At least 1 on OLD and 0 on NEW. The pool did not follow the alias. | 1 |
| `GONE` | 0 on both servers. The node left OLD and nothing proves it arrived. | 3 |
| `UNSETTLED` | The last round is clean, but an earlier round of the window was not 0 on OLD with at least 1 on NEW. | 3 |
| `INCONCLUSIVE` | Too few rounds, a missing or blind poll, or no warm baseline. | 3 |

The baseline must show the node on OLD and not on NEW. A node with no session on OLD at baseline
had nothing warm across the flip, so there was nothing to observe. A node that was already on
NEW at baseline did not move because of the flip.

A `MIGRATED` node still reports the rounds where it was split on its way. Here, `app-01` passed
through one split round, and `app-02` kept two sessions on OLD to the end:

```
$ python sessorigin.py rehearsal.csv --old DBOLD --new DBNEW
sessorigin: rehearsal.csv
rounds 1-6, OLD = DBOLD, NEW = DBNEW, settle 2
  app-01  MIGRATED      0 on OLD and at least 4 on NEW in each of the last 2 round(s)
                        passed through a split in 1 round(s): 3, at 2026-01-05 10:02:00.0000000
  app-02  SPLIT         2 session(s) still on OLD and 2 on NEW in round 6
                        split in 4 round(s): 3-6, from 2026-01-05 10:02:00.0000000 to 2026-01-05 10:05:00.0000000

VERDICT: SPLIT. 1 node(s) answer from both databases. This is not a migration.
```

After a full restart of the service, two more rounds show both nodes only on NEW:

```
$ python sessorigin.py after-restart.csv --old DBOLD --new DBNEW
sessorigin: after-restart.csv
rounds 1-8, OLD = DBOLD, NEW = DBNEW, settle 2
  app-01  MIGRATED      0 on OLD and at least 4 on NEW in each of the last 2 round(s)
                        passed through a split in 1 round(s): 3, at 2026-01-05 10:02:00.0000000
  app-02  MIGRATED      0 on OLD and at least 4 on NEW in each of the last 2 round(s)
                        passed through a split in 4 round(s): 3-6, from 2026-01-05 10:02:00.0000000 to 2026-01-05 10:05:00.0000000

VERDICT: MIGRATED. Every node is on NEW and none is on OLD.
```

The same two rounds, polled from OLD without the permission, prove nothing:

```
$ python sessorigin.py blind.csv --old DBOLD --new DBNEW
sessorigin: blind.csv
rounds 1-8, OLD = DBOLD, NEW = DBNEW, settle 2
  app-01  INCONCLUSIVE  round 7: the poll of OLD could not see other sessions (view_server_state is not 1), so its zero proves nothing
                        split in 1 round(s): 3, at 2026-01-05 10:02:00.0000000
  app-02  INCONCLUSIVE  round 7: the poll of OLD could not see other sessions (view_server_state is not 1), so its zero proves nothing
                        split in 4 round(s): 3-6, from 2026-01-05 10:02:00.0000000 to 2026-01-05 10:05:00.0000000

VERDICT: NOT CERTIFIED. 2 of 2 node(s) cannot be called migrated.
```

These runs read synthetic CSV files that are not shipped. The self-test builds each of these
exports, runs the tool on it, and compares every printed line with the lines shown here. Only the
line that names the file differs. It does the same for both preflight runs in the next section.

## The preflight

A rehearsal can repoint the alias for one node only, with a line in that node's hosts file. The
edit can report success while the node's resolver still answers with the old address. Reading
the file back does not detect this, because the file is correct. Only the resolver's answer is
the fact. If the resolver answers the old address, a session verdict tests nothing: the pool
"stayed" because the node never saw the flip.

Run `Resolve-DnsName` for the alias on the node, then give each address it returns to
`--resolved`. A copy of the node's hosts file is optional evidence of what was written.

```
$ python sessorigin.py --alias db.example.test --expect 198.51.100.20 --resolved 192.0.2.10 --hosts-file hosts.txt
hosts file: line 3 maps db.example.test to 198.51.100.20
PREFLIGHT FAIL  the hosts edit is on disk and not in effect: the file maps db.example.test to 198.51.100.20, and the resolver still answers 192.0.2.10.
  A session verdict now would test the old address, not the flip.
```

The preflight passes only when the resolver returns the expected address and no other. If it
returns the new address and the old one, it fails, because a client can connect to either.
Addresses compare as addresses, so `2001:DB8::20` equals `2001:db8::20`. The hosts file is read
as `hosts(5)` describes it: an address, then names, separated by blanks or tabs, and `#` starts
a comment. Names compare without case, as DNS names do. A hosts line whose first field is not an
address is named in the output, because no resolver can use it.

With a session export, a preflight speaks for one node, so name that node with exactly one
`--node`. A failed preflight withholds every session verdict, exits 3, and writes nothing:

```
$ python sessorigin.py rehearsal.csv --old DBOLD --new DBNEW --node app-02 --alias db.example.test --expect 198.51.100.20 --resolved 192.0.2.10 --hosts-file hosts.txt
hosts file: line 3 maps db.example.test to 198.51.100.20
PREFLIGHT FAIL  the hosts edit is on disk and not in effect: the file maps db.example.test to 198.51.100.20, and the resolver still answers 192.0.2.10.
  A session verdict now would test the old address, not the flip.

Session verdicts withheld: the node never resolved the alias to the new address.
```

The tool does not explain why a hosts edit is not in effect. It reports the symptom only.

## The alias flip nobody tested

The first cutover plan republished every service after the alias flip. Under that order, no
service was ever warm across a flip, so nobody had seen what a warm pool does. A later plan
published services first and flipped the alias afterwards. That plan assumes that running
services follow the alias.

ArcGIS Server keeps service instances alive between requests. It recycles them on a schedule,
by default every 24 hours, one instance at a time, and an instance in use is not recycled until
it is released. In the rehearsal, an open connection stayed on the server it was opened against.
So after a flip, a warm service can keep its old connections until a recycle or a restart.

The rehearsal confirmed it. The warm pool did not drain by itself. A per-service recycle moved
most of the connections and left a few behind. Only a full restart of the ArcGIS Server service
on the node cleared the old server. The runbook changed in two places:

1. Restart the ArcGIS Server service on every node right after the flip. A per-service recycle
   is not a substitute.
2. Verify the cutover by connection origin: zero sessions from any node on the old server. A
   feature count or a REST check is a liveness check, not a cutover check.

This tool is the second step, with the trap closed: a node with sessions on both servers is
never called migrated. The restart in the first step uses the operating system's service manager
on each node. This tool does not do it. [svcguard](https://github.com/uhsear/svcguard) stops and
starts single services through the Admin API, which is the per-service recycle that left sessions
behind here, so it is not a substitute for the node restart.

## Existing tools

[sp_WhoIsActive](https://github.com/amachanic/sp_whoisactive) is a comprehensive activity
monitor for SQL Server, under GPLv3. dbatools
[`Get-DbaProcess`](https://dbatools.io/Get-DbaProcess) lists sessions with their host, program
and login, and it filters by `-Hostname`. Both show you live sessions on one server better than
this tool does, and with much more detail. Neither compares two servers over time, and neither
gives a verdict. The raw DMV gives the same rows with no verdict.

On 2026-10-09, before this repository was public, `gh search repos` returned no repository for
`sessorigin`, for "alias flip sql sessions", for "dm_exec_sessions cutover", or for "split brain
dns alias sql server". On 2026-10-13 the same searches return only this repository, for its own
name. That shows only what those searches found.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Every node is `MIGRATED`, or a preflight run on its own passed. |
| 1 | At least one node is `SPLIT` or `STAYED`: sessions remain on the old server. |
| 2 | A file could not be read or written, or a row is malformed. argparse also exits 2 for an unknown flag, such as `--ap`, and for a bad value, such as `--settle abc`. |
| 3 | Nothing is certified: `GONE`, `UNSETTLED`, `INCONCLUSIVE`, no node at all, a failed preflight, or a host that `--node` left out and that still shows sessions on OLD. |
| 64 | Usage error, including a server name or a column that is not in the export, and an `--out` that names a file the run reads. |

Exit 1 is above exit 3 when both apply. A split node needs action now. An unproven node needs
another round of polls. An uncaught Python error also exits 1, so read stderr when exit 1 comes
with no `VERDICT` line.

## Limits

- It does not poll anything. It reads a CSV. The export query above has not been run against a
  SQL Server by these tests.
- `host_name` is supplied by the client and can be wrong. Microsoft says not to rely on it as a
  security feature. The tool trusts it to name the node.
- The preflight takes the resolver's answer as input. It does not resolve anything itself,
  because the answer that matters is the node's, not the workstation's.
- It does not know when the flip happened. The lowest round is the baseline, so poll once before
  you flip.
- It does not compare `login_time`. A session opened on the old server after the flip could show
  that the node still resolves the old address. That check is not built.
- `poll_time` is printed as text. Rounds order by `round`, never by time.
- A split in a round where either poll was missing or blind is not counted in the split history.
- `--settle 2` is a default, not a standard. One round of zero can fall between two recycled
  connections.
- It reads ASCII or UTF-8 only. UTF-16 files are refused.
- Outside `--self-test`, nothing is written without `--apply`, and a withheld run writes
  nothing. The self-test writes fixture files to a temporary directory and removes it. The tool
  opens no socket and holds no credential.
- "Every node" means every host in the export. A node that the export query's host filter left
  out is never judged. Name the full set with `--node`, or widen the filter.
- `--out` replaces an existing file without asking, and the write is not atomic. A failure in the
  middle leaves a short file. The tool refuses an `--out` that names the export or the hosts
  file.
- The `--out` file is UTF-8 and holds the host name as the export spells it. A name that starts
  with `=`, `+`, `-` or `@` gets a leading quote, so a spreadsheet shows it as text. Treat
  `host_name` as untrusted text anyway.
- Printed text escapes every non-ASCII character as `\uXXXX` (or `\UXXXXXXXX`), so a console that cannot encode a
  host name still gets the report. A file that is not UTF-8 decodes with replacement characters.
- Error messages quote the cell that caused them. Do not share them from an export that holds
  more than poll rows.
- An uncaught Python error exits 1, the same code as `SPLIT` and `STAYED`. Read stderr when exit 1
  comes with no `VERDICT` line.
- A CSV cut off in the middle of a poll is not detected, because a poll has no end marker. If the
  rows of OLD for one node are cut off and the rest of that poll survives, the poll still counts
  as read, and the node can read as `MIGRATED`. Check that the export is whole before you trust a
  verdict.
- Rounds are the whole numbers that Python `int()` accepts, so `01` and `1` are one round, and
  `1_0` is round 10. Rows that repeat a round are merged, so a poll appended twice can add
  sessions that have since closed. That errs toward `SPLIT` or `INCONCLUSIVE`.
- The settle window is the last `--settle` rounds that are present, not consecutive round
  numbers. A gap such as 1, 2, 50 is not detected.
- A hosts line that names the alias with a trailing dot (`db.example.test.`) does not match. An
  IPv4-mapped IPv6 answer (`::ffff:198.51.100.20`) does not equal the IPv4 address. A hosts line
  with an IPv6 zone id, such as `fe80::1%eth0`, reads as not an address on Python 3.9. All three
  fail the preflight, which is the safe direction.
- A header line that joins two exports is refused. The error names a repeated header row only
  when the server cell is blank or reads `server`.
- Sessions count per client `host_name`. Any process that connects from that name counts as the
  node, not only ArcGIS Server.
- The search for an equivalent tool found none on 2026-10-09, and finds only this repository
  for its own name now. That is not proof that none exists.

## Sources

- [sys.dm_exec_sessions](https://learn.microsoft.com/en-us/sql/relational-databases/system-dynamic-management-views/sys-dm-exec-sessions-transact-sql):
  the `host_name`, `session_id` and `is_user_process` columns, the security note on
  `host_name`, and the permissions ("Everyone can see their own session information").
- [HAS_PERMS_BY_NAME](https://learn.microsoft.com/en-us/sql/t-sql/functions/has-perms-by-name-transact-sql):
  returns 1 or 0, and example A checks `VIEW SERVER STATE` with `NULL, NULL`.
- [@@SPID](https://learn.microsoft.com/en-us/sql/t-sql/functions/spid-transact-sql):
  returns the session ID of the current user process.
- [Invoke-Sqlcmd](https://learn.microsoft.com/en-us/powershell/module/sqlserver/invoke-sqlcmd):
  the `-Variable` parameter sets sqlcmd scripting variables for the script.
- [@@SERVERNAME](https://learn.microsoft.com/en-us/sql/t-sql/functions/servername-transact-sql):
  the name of the local server that runs SQL Server.
- [sqlcmd scripting variables](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-use-scripting-variables):
  the `$(name)` form.
- [Export-Csv](https://learn.microsoft.com/en-us/powershell/module/microsoft.powershell.utility/export-csv):
  the `#TYPE` header is the default before PowerShell 6.0, and values convert with `ToString()`.
- [DBNull.ToString](https://learn.microsoft.com/en-us/dotnet/api/system.dbnull.tostring):
  returns an empty string.
- [Tuning and configuring services](https://enterprise.arcgis.com/en/server/latest/publish-services/windows/tuning-and-configuring-services.htm):
  the 24-hour default recycling interval, one instance at a time, and instances in use wait.
- [hosts(5)](https://man7.org/linux/man-pages/man5/hosts.5.html): the hosts file format.
- [RFC 4343](https://www.rfc-editor.org/rfc/rfc4343): DNS names are case insensitive.
- [RFC 5737](https://www.rfc-editor.org/rfc/rfc5737): the documentation addresses used in every
  example here.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [svcguard](https://github.com/uhsear/svcguard) - stop and start single ArcGIS Server services
  around risky maintenance, even on failure. It works per service, so it is not the node-wide
  restart that cleared the split here.
- [svcsource](https://github.com/uhsear/svcsource) - report the database and instance behind
  every service. It names the database a service reads, not the nodes that serve it.
- [stalehost](https://github.com/uhsear/stalehost) - find every ArcGIS file that still names the
  host you are retiring.
- [compressfloor](https://github.com/uhsear/compressfloor) - the same exported-rows pattern,
  applied to the geodatabase compress floor.
