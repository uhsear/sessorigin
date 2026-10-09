#!/usr/bin/env python
"""Give a verdict from exported session rows after a database alias flip.

Never reports MIGRATED while one node still holds sessions on the old server.

A DNS alias moves from the old SQL Server to the new one. Warm connection pools
keep their open connections, and an open connection does not look at DNS again.
A node can then answer some requests from the old database and some from the
new one, return HTTP 200 for both, and log nothing. Feature counts cannot see
it, because at cutover the two databases hold the same rows. The only reliable
witness is the server side: which server holds the node's sessions.

This tool reads sys.dm_exec_sessions rows polled from BOTH servers, round by
round, and gives one verdict per node: MIGRATED, SPLIT, STAYED, GONE, UNSETTLED
or INCONCLUSIVE. MIGRATED needs zero sessions on the old server and at least
one on the new server in every one of the last --settle rounds.

A preflight checks the experiment itself. A hosts-file edit can be on disk and
not in effect, so the node keeps resolving the alias to the old address. Give
the resolver's own answer with --resolved, and the tool refuses to give any
session verdict when that answer is not the new address.

    python sessorigin.py --self-test
    python sessorigin.py polls.csv --old DBOLD --new DBNEW
    python sessorigin.py polls.csv --old DBOLD --new DBNEW --node app-01 --settle 3
    python sessorigin.py --alias db.example.test --expect 198.51.100.20 \\
        --resolved 192.0.2.10 --hosts-file hosts.txt
    python sessorigin.py polls.csv --old DBOLD --new DBNEW --out verdicts.csv --apply

Nothing is written without --apply.

Exit codes: 0 every node MIGRATED (or the preflight passed), 1 at least one node
still holds sessions on the old server (SPLIT or STAYED), 2 a file could not be
read or written, 3 nothing can be certified (INCONCLUSIVE, GONE, UNSETTLED, or a
failed preflight), 64 usage error.
"""

import argparse
import contextlib
import csv
import io
import ipaddress
import os
import runpy
import shutil
import sys
import tempfile

# =============================================================================
# CONFIGURATION. Deliberately not flags, except where a flag is named.
# =============================================================================

# How many trailing rounds must all show the node only on the new server before
# MIGRATED is reported. One round is not enough: a poll can land in the moment
# between two recycled connections. --settle overrides it.
DEFAULT_SETTLE = 2

# The columns the export must carry. Matched without regard to case.
REQUIRED_COLUMNS = ("round", "server", "host_name", "session_id",
                    "view_server_state")
# Echoed verbatim when present, never parsed.
OPTIONAL_COLUMNS = ("poll_time",)

OUT_COLUMNS = ("host", "verdict", "reason", "baseline_old", "final_old",
               "final_new", "split_rounds", "first_round_on_new")

# Verdicts, in the order the exit code weighs them.
MIGRATED = "MIGRATED"
SPLIT = "SPLIT"
STAYED = "STAYED"
GONE = "GONE"
UNSETTLED = "UNSETTLED"
INCONCLUSIVE = "INCONCLUSIVE"

ROLE_NAME = {"old": "OLD", "new": "NEW"}


class UsageError(ValueError):
    """The operator named something the input does not contain."""


class BadInput(ValueError):
    """The input itself is malformed."""


# ------------------------------------------------------------- decision core

class Poll(object):
    """One server polled in one round."""

    def __init__(self):
        self.blind = False
        self.time = ""
        self.sessions = {}

    def count(self, host):
        return len(self.sessions.get(host, ()))


def header_map(fieldnames):
    """Lower-cased, stripped column name to the name the file spells."""
    out = {}
    for name in fieldnames:
        out.setdefault(name.strip().lower(), name)
    return out


def parse_export(text):
    """Records of an exported poll CSV, as (line number, {column: value}).

    Windows PowerShell 5.1 Export-Csv writes a '#TYPE ...' line above the
    header unless -NoTypeInformation is given. Read as data, that line becomes
    the header and every real column reads as absent.
    """
    offset = 0
    if text.startswith("#TYPE"):
        newline = text.find("\n")
        text = "" if newline < 0 else text[newline + 1:]
        offset = 1
    reader = csv.DictReader(io.StringIO(text))
    names = header_map(reader.fieldnames or [])
    absent = [c for c in REQUIRED_COLUMNS if c not in names]
    if absent:
        raise UsageError(
            "columns not in the export: %s. It has: %s"
            % (", ".join(absent),
               ", ".join(reader.fieldnames) if reader.fieldnames
               else "(no columns at all)"))
    records = []
    for raw in reader:
        rec = {}
        for col in REQUIRED_COLUMNS + OPTIONAL_COLUMNS:
            value = raw.get(names.get(col, ""))
            rec[col] = "" if value is None else str(value).strip()
        records.append((reader.line_num + offset, rec))
    return records


def build_polls(records, old, new):
    """Group records into {round: {"old": Poll, "new": Poll}}.

    A record with a blank session_id is a poll marker: it proves the server
    was polled in that round even when no session matched. A poll whose
    view_server_state is not 1 is blind, because without that permission
    sys.dm_exec_sessions shows the caller only its own session, and every
    node would look as if it had left.
    """
    polls = {}
    labels = {}
    unknown = []
    seen = set()
    for line, rec in records:
        server = rec["server"]
        if server.lower() == old.lower():
            role = "old"
        elif server.lower() == new.lower():
            role = "new"
        else:
            if server not in unknown:
                unknown.append(server)
            continue
        seen.add(role)
        try:
            rnd = int(rec["round"])
        except ValueError:
            raise BadInput("line %d: round %r is not a whole number"
                           % (line, rec["round"]))
        poll = polls.setdefault(rnd, {}).setdefault(role, Poll())
        if rec["view_server_state"] != "1":
            poll.blind = True
        if not poll.time:
            poll.time = rec["poll_time"]
        if not rec["session_id"]:
            continue
        key = rec["host_name"].lower()
        labels.setdefault(key, rec["host_name"] or "(blank host_name)")
        poll.sessions.setdefault(key, set()).add(rec["session_id"])
    if unknown:
        raise UsageError(
            "server(s) %s are on rows but are neither --old %r nor --new %r"
            % (", ".join(repr(s) for s in unknown), old, new))
    for role, name in (("old", old), ("new", new)):
        if role not in seen:
            raise UsageError(
                "--%s %r is on no row. Poll each server by its own name, never "
                "through the alias: after the flip the alias answers as the "
                "new server, and @@SERVERNAME says so." % (role, name))
    return polls, labels


def spans(numbers):
    """[3, 4, 5, 9] as '3-5, 9'."""
    parts = []
    start = prev = None
    for n in numbers:
        if start is None:
            start = prev = n
        elif n == prev + 1:
            prev = n
        else:
            parts.append(str(start) if start == prev else
                         "%d-%d" % (start, prev))
            start = prev = n
    if start is not None:
        parts.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ", ".join(parts)


class Verdict(object):
    def __init__(self, host, label):
        self.host = host
        self.label = label
        self.verdict = INCONCLUSIVE
        self.reason = ""
        self.baseline_old = None
        self.final_old = None
        self.final_new = None
        self.split_rounds = []
        self.split_times = ("", "")
        self.first_new = None


def _unreadable(round_polls, rnd):
    """The role whose poll in this round is missing or blind, or None."""
    for role in ("old", "new"):
        poll = round_polls.get(role)
        if poll is None:
            return "round %d has no poll of %s" % (rnd, ROLE_NAME[role])
        if poll.blind:
            return ("round %d: the poll of %s could not see other sessions "
                    "(view_server_state is not 1), so its zero proves nothing"
                    % (rnd, ROLE_NAME[role]))
    return None


def judge(host, label, polls, settle):
    """One node's verdict over every round. Pure."""
    v = Verdict(host, label)
    rounds = sorted(polls)
    readable = [r for r in rounds if _unreadable(polls[r], r) is None]
    for r in readable:
        o, n = polls[r]["old"].count(host), polls[r]["new"].count(host)
        if o and n:
            v.split_rounds.append(r)
        if n and v.first_new is None:
            v.first_new = r
    if v.split_rounds:
        v.split_times = (polls[v.split_rounds[0]]["old"].time,
                         polls[v.split_rounds[-1]]["old"].time)

    if not any(p.count(host) for r in rounds for p in polls[r].values()):
        v.reason = ("never seen in any poll. Check the host filter in the "
                    "export query")
        return v
    if len(rounds) < 1 + settle:
        v.reason = ("%d round(s); a verdict needs a baseline round and %d "
                    "more" % (len(rounds), settle))
        return v
    base = rounds[0]
    problem = _unreadable(polls[base], base)
    if problem:
        v.reason = "baseline " + problem
        return v
    v.baseline_old = polls[base]["old"].count(host)
    base_new = polls[base]["new"].count(host)
    if base_new:
        v.reason = ("already had %d session(s) on NEW in the baseline round "
                    "%d, so the flip cannot be what moved it" % (base_new, base))
        return v
    if not v.baseline_old:
        v.reason = ("no session on OLD in the baseline round %d: nothing was "
                    "warm across the flip, so there was nothing to observe"
                    % base)
        return v

    window = rounds[-settle:]
    for r in window:
        problem = _unreadable(polls[r], r)
        if problem:
            v.reason = problem
            return v
    last = window[-1]
    v.final_old = polls[last]["old"].count(host)
    v.final_new = polls[last]["new"].count(host)
    if v.final_old and v.final_new:
        v.verdict = SPLIT
        v.reason = ("%d session(s) still on OLD and %d on NEW in round %d"
                    % (v.final_old, v.final_new, last))
    elif v.final_old:
        v.verdict = STAYED
        v.reason = ("%d session(s) on OLD and none on NEW in round %d: the "
                    "pool did not follow the alias" % (v.final_old, last))
    elif not v.final_new:
        v.verdict = GONE
        v.reason = ("no session on either server in round %d: it left OLD "
                    "and never arrived, so nothing proves it moved" % last)
    elif all(not polls[r]["old"].count(host) and polls[r]["new"].count(host)
             for r in window):
        v.verdict = MIGRATED
        v.reason = ("0 on OLD and %d on NEW in each of the last %d round(s)"
                    % (v.final_new, settle))
    else:
        v.verdict = UNSETTLED
        v.reason = ("OLD reached zero inside the last %d round(s), not across "
                    "all of them. Poll again" % settle)
    return v


def assess(polls, labels, nodes, settle):
    """Verdicts for the named nodes, or for every host seen. Pure."""
    if nodes:
        hosts = []
        for name in nodes:
            key = name.strip().lower()
            if key not in hosts:
                hosts.append(key)
                labels.setdefault(key, name.strip())
    else:
        hosts = sorted(labels)
    return [judge(h, labels[h], polls, settle) for h in hosts]


def gate(verdicts):
    """Exit code: 1 sessions remain on OLD, 3 nothing certified, 0 migrated."""
    kinds = set(v.verdict for v in verdicts)
    if kinds & set([SPLIT, STAYED]):
        return 1
    if not verdicts or kinds != set([MIGRATED]):
        return 3
    return 0


def overall(verdicts):
    kinds = [v.verdict for v in verdicts]
    if SPLIT in kinds:
        return ("SPLIT. %d node(s) answer from both databases. This is not a "
                "migration." % kinds.count(SPLIT))
    if STAYED in kinds:
        return ("STAYED. %d node(s) still answer only from OLD."
                % kinds.count(STAYED))
    if not verdicts:
        return "INCONCLUSIVE. No node session is in any round."
    if set(kinds) != set([MIGRATED]):
        return ("NOT CERTIFIED. %d of %d node(s) cannot be called migrated."
                % (len(kinds) - kinds.count(MIGRATED), len(kinds)))
    return "MIGRATED. Every node is on NEW and none is on OLD."


def describe(verdicts, polls, old, new, settle):
    rounds = sorted(polls)
    lines = ["rounds %s, OLD = %s, NEW = %s, settle %d"
             % (spans(rounds) or "none", old, new, settle)]
    width = max([len(v.label) for v in verdicts] + [4])
    for v in verdicts:
        lines.append("  %-*s  %-12s  %s" % (width, v.label, v.verdict,
                                            v.reason))
        if v.split_rounds:
            when = ""
            if v.split_times[0] == v.split_times[1]:
                when = v.split_times[0] and ", at %s" % v.split_times[0]
            else:
                when = ", from %s to %s" % v.split_times
            lead = ("passed through a split in" if v.verdict == MIGRATED
                    else "split in")
            lines.append("  %-*s  %-12s  %s %d round(s): %s%s"
                         % (width, "", "", lead, len(v.split_rounds),
                            spans(v.split_rounds), when))
    lines.append("")
    lines.append("VERDICT: " + overall(verdicts))
    return lines


def out_rows(verdicts):
    for v in verdicts:
        yield {
            "host": v.label,
            "verdict": v.verdict,
            "reason": v.reason,
            "baseline_old": "" if v.baseline_old is None else v.baseline_old,
            "final_old": "" if v.final_old is None else v.final_old,
            "final_new": "" if v.final_new is None else v.final_new,
            "split_rounds": spans(v.split_rounds),
            "first_round_on_new": "" if v.first_new is None else v.first_new,
        }


# ------------------------------------------------------- hostsblock preflight

def _address(text, what):
    try:
        return ipaddress.ip_address(text.strip())
    except ValueError:
        raise UsageError("%s %r is not an IP address" % (what, text))


def hosts_lines(text, alias):
    """Active hosts-file lines that name the alias: (line, first field).

    Format per hosts(5): an address, then names, separated by blanks or tabs;
    '#' starts a comment. Names compare without case (RFC 4343).
    """
    found = []
    for number, raw in enumerate(text.splitlines(), 1):
        fields = raw.split("#", 1)[0].split()
        if len(fields) > 1 and alias.lower() in [f.lower() for f in fields[1:]]:
            found.append((number, fields[0]))
    return found


def preflight(alias, expect, resolved, hosts_text=None):
    """Is the node's resolver answering the new address? Pure.

    Returns (passed, lines). The file is evidence of intent only. The
    resolver's answer is the fact.
    """
    want = _address(expect, "--expect")
    got = []
    for r in resolved:
        a = _address(r, "--resolved")
        if a not in got:
            got.append(a)
    lines = []
    on_disk = []
    if hosts_text is not None:
        entries = hosts_lines(hosts_text, alias)
        if not entries:
            lines.append("hosts file: no active line names %s" % alias)
        for number, first in entries:
            try:
                addr = ipaddress.ip_address(first)
            except ValueError:
                lines.append("hosts file: line %d names %s, but %r is not an "
                             "address, so no resolver can use it"
                             % (number, alias, first))
                continue
            if addr == want:
                on_disk.append(number)
                lines.append("hosts file: line %d maps %s to %s"
                             % (number, alias, addr))
            else:
                lines.append("hosts file: line %d maps %s to %s, not %s"
                             % (number, alias, addr, want))
    answer = ", ".join(str(a) for a in got)
    if got == [want]:
        lines.append("PREFLIGHT PASS  the resolver answers %s for %s"
                     % (want, alias))
        return True, lines
    if want in got:
        lines.append("PREFLIGHT FAIL  the resolver answers %s for %s. A client "
                     "may connect to any of them." % (answer, alias))
    elif on_disk:
        lines.append("PREFLIGHT FAIL  the hosts edit is on disk and not in "
                     "effect: the file maps %s to %s, and the resolver still "
                     "answers %s." % (alias, want, answer))
    else:
        lines.append("PREFLIGHT FAIL  the resolver answers %s for %s, not %s."
                     % (answer, alias, want))
    lines.append("  A session verdict now would test the old address, not "
                 "the flip.")
    return False, lines


def read_text(path):
    """A text file as str. Refuses UTF-16 instead of misreading it."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:2] in (b"\xff\xfe", b"\xfe\xff") or b"\x00" in data:
        raise BadInput("%s is UTF-16 or holds NUL bytes. Save it as ASCII or "
                       "UTF-8 and run again." % path)
    return data.decode("utf-8-sig", "replace")


# ------------------------------------------------------------------ self-test

def self_test():
    """Assertions over the decision core and the CLI. No database, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label, kind=ValueError):
        try:
            fn()
        except kind:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    def message(fn, kind=ValueError):
        """The text of the exception fn raises, or '' when it raises none."""
        try:
            fn()
        except kind as exc:
            return str(exc)
        return ""

    head = "round,server,host_name,session_id,view_server_state,poll_time\n"

    def poll(rnd, server, sessions, vss="1", t=""):
        """CSV lines for one server in one round. sessions: [(host, n)]."""
        out = []
        sid = 50
        for host, n in sessions:
            for _ in range(n):
                sid += 1
                out.append("%d,%s,%s,%d,%s,%s\n" % (rnd, server, host, sid,
                                                    vss, t))
        if not out:
            out.append("%d,%s,,,%s,%s\n" % (rnd, server, vss, t))
        return "".join(out)

    def series(pairs, host="app-01"):
        """[(old n, new n), ...] as one export, round 1 first."""
        text = head
        for i, (o, n) in enumerate(pairs, 1):
            text += poll(i, "DBOLD", [(host, o)] if o else [])
            text += poll(i, "DBNEW", [(host, n)] if n else [])
        return text

    def verdicts(text, nodes=None, settle=DEFAULT_SETTLE):
        polls, labels = build_polls(parse_export(text), "DBOLD", "DBNEW")
        return assess(polls, labels, nodes, settle)

    def one(text, **kw):
        return verdicts(text, **kw)[0]

    print("sessorigin self-test: no database, no network, no credentials")
    print("-" * 68)

    # ---- the defect this tool exists for
    v = one(series([(8, 0), (8, 0), (2, 6), (2, 6), (2, 6)]))
    check(v.verdict == SPLIT,
          "sessions on NEW while 2 remain on OLD is SPLIT, never MIGRATED"
          "  <-- pinned defect")
    check("2 session(s) still on OLD and 6 on NEW in round 5" in v.reason,
          "and the reason names both counts and the round")
    check(gate([v]) == 1, "and SPLIT fails the gate with exit 1")
    check(v.split_rounds == [3, 4, 5], "the split rounds are 3, 4 and 5")
    check(v.first_new == 3, "the first round with a session on NEW is 3")
    v = one(series([(8, 0), (1, 7), (1, 7)]))
    check(v.verdict == SPLIT,
          "one survivor on OLD beside seven on NEW is still SPLIT"
          "  <-- pinned defect")

    # ---- the clean outcomes
    v = one(series([(4, 0), (4, 0), (2, 2), (0, 4), (0, 4)]))
    check(v.verdict == MIGRATED,
          "zero on OLD and four on NEW for the last two rounds is MIGRATED")
    check(gate([v]) == 0, "and MIGRATED passes the gate")
    check(v.split_rounds == [3],
          "and the split it passed through in round 3 is still recorded")
    polls, _ = build_polls(parse_export(
        series([(4, 0), (4, 0), (2, 2), (0, 4), (0, 4)])), "DBOLD", "DBNEW")
    lines = describe([v], polls, "DBOLD", "DBNEW", 2)
    check(any("passed through a split in 1 round(s): 3" in ln for ln in lines),
          "and the report says a MIGRATED node passed through a split")
    timed = head
    for r, (o, n) in enumerate([(8, 0), (2, 6), (2, 6)], 1):
        t = "2026-01-05 10:0%d:00.0000000" % r
        timed += poll(r, "DBOLD", [("app-01", o)], t=t)
        timed += poll(r, "DBNEW", [("app-01", n)] if n else [], t=t)
    polls, labels = build_polls(parse_export(timed), "DBOLD", "DBNEW")
    lines = describe(assess(polls, labels, None, 2), polls, "DBOLD", "DBNEW",
                     2)
    check(any("split in 2 round(s): 2-3, from 2026-01-05 10:02:00.0000000 to "
              "2026-01-05 10:03:00.0000000" in ln for ln in lines),
          "a split prints its rounds and the poll_time text, unparsed")
    timed = timed.replace("3,DBOLD,app-01,51,1,", "3,DBOLD,,,1,")
    timed = timed.replace("3,DBOLD,app-01,52,1,2026-01-05 10:03:00.0000000\n",
                          "")
    polls, labels = build_polls(parse_export(timed), "DBOLD", "DBNEW")
    lines = describe(assess(polls, labels, None, 1), polls, "DBOLD", "DBNEW",
                     1)
    check(any("passed through a split in 1 round(s): 2, at 2026-01-05 "
              "10:02:00.0000000" == ln.strip() for ln in lines),
          "a one-round split prints one time, not a range from it to itself")
    v = one(series([(4, 0), (4, 0), (4, 0), (4, 0)]))
    check(v.verdict == STAYED, "a pool that never moves is STAYED")
    check("did not follow the alias" in v.reason,
          "and the reason says the pool did not follow")
    check(gate([v]) == 1, "and STAYED fails the gate with exit 1")
    v = one(series([(4, 0), (2, 0), (0, 0), (0, 0)]))
    check(v.verdict == GONE,
          "a node with no session anywhere at the end is GONE, not MIGRATED"
          "  <-- pinned defect")
    check(gate([v]) == 3, "and GONE certifies nothing: exit 3")

    # ---- settle
    v = one(series([(4, 0), (4, 0), (2, 2), (0, 4)]))
    check(v.verdict == UNSETTLED,
          "zero on OLD in the final round only is UNSETTLED at settle 2"
          "  <-- pinned defect")
    check(gate([v]) == 3, "and UNSETTLED exits 3")
    v = one(series([(4, 0), (4, 0), (2, 2), (0, 4)]), settle=1)
    check(v.verdict == MIGRATED, "the same rounds at settle 1 are MIGRATED")
    v = one(series([(4, 0), (0, 4), (1, 3), (0, 4)]))
    check(v.verdict == UNSETTLED,
          "a node that went back to OLD inside the window is UNSETTLED")
    v = one(series([(4, 0), (0, 4)]))
    check(v.verdict == INCONCLUSIVE and "needs a baseline round and 2" in
          v.reason, "two rounds at settle 2 are too few: INCONCLUSIVE")

    # ---- the baseline
    v = one(series([(0, 0), (0, 3), (0, 3)]))
    check(v.verdict == INCONCLUSIVE and "nothing was warm" in v.reason,
          "no session on OLD at baseline is INCONCLUSIVE, not MIGRATED"
          "  <-- pinned defect")
    v = one(series([(3, 1), (0, 4), (0, 4)]))
    check(v.verdict == INCONCLUSIVE and "already had 1 session(s) on NEW" in
          v.reason, "a node already on NEW at baseline is INCONCLUSIVE"
          "  <-- pinned defect")
    text = head + poll(1, "DBOLD", [("app-01", 3)])
    text += poll(2, "DBOLD", []) + poll(2, "DBNEW", [("app-01", 3)])
    text += poll(3, "DBOLD", []) + poll(3, "DBNEW", [("app-01", 3)])
    v = one(text)
    check(v.verdict == INCONCLUSIVE and v.reason.startswith(
        "baseline round 1 has no poll of NEW"),
        "a baseline round with no poll of NEW is INCONCLUSIVE")

    # ---- blind and missing polls
    text = head + poll(1, "DBOLD", [("app-01", 4)]) + poll(1, "DBNEW", [])
    for r in (2, 3):
        text += poll(r, "DBOLD", [], vss="0") + poll(r, "DBNEW",
                                                     [("app-01", 4)])
    v = one(text)
    check(v.verdict == INCONCLUSIVE and "could not see other sessions" in
          v.reason,
          "a poll of OLD without VIEW SERVER STATE is blind, not a zero"
          "  <-- pinned defect")
    text = head + poll(1, "DBOLD", [("app-01", 4)]) + poll(1, "DBNEW", [])
    text += poll(2, "DBOLD", []) + poll(2, "DBNEW", [("app-01", 4)])
    text += poll(3, "DBNEW", [("app-01", 4)])
    v = one(text)
    check(v.verdict == INCONCLUSIVE and v.reason ==
          "round 3 has no poll of OLD",
          "a final round with no poll of OLD is INCONCLUSIVE, not MIGRATED"
          "  <-- pinned defect")
    text = head + poll(1, "DBOLD", [("app-01", 4)]) + poll(1, "DBNEW", [])
    text += poll(2, "DBOLD", [("app-01", 2)]) + poll(2, "DBNEW",
                                                    [("app-01", 2)])
    text += poll(3, "DBOLD", []) + poll(3, "DBNEW", [("app-01", 4)], vss="0")
    text += poll(4, "DBOLD", []) + poll(4, "DBNEW", [("app-01", 4)])
    text += poll(5, "DBOLD", []) + poll(5, "DBNEW", [("app-01", 4)])
    v = one(text)
    check(v.verdict == MIGRATED and v.split_rounds == [2],
          "a blind round outside the window does not block MIGRATED")
    text = head + poll(1, "DBOLD", [("app-01", 4)], vss="1")
    text += "1,DBOLD,,,0,\n" + poll(1, "DBNEW", [])
    text += poll(2, "DBOLD", []) + poll(2, "DBNEW", [("app-01", 4)])
    text += poll(3, "DBOLD", []) + poll(3, "DBNEW", [("app-01", 4)])
    v = one(text)
    check(v.verdict == INCONCLUSIVE and "baseline round 1: the poll of OLD"
          in v.reason,
          "one blind row makes the whole poll of that server blind")

    # ---- the export
    text = series([(2, 0), (2, 0), (0, 2), (0, 2)])
    dup = text.replace("4,DBNEW,app-01,51,1,\n",
                       "4,DBNEW,app-01,51,1,\n" * 3)
    check(dup.count("4,DBNEW,app-01,51,1,") == 3, "the repeat is in the file")
    check(one(dup).final_new == 2,
          "a session row repeated in one poll is counted once")
    mixed = text.replace("3,DBNEW,app-01,51", "3,DBNEW,APP-01,51")
    vs = verdicts(mixed)
    check(len(vs) == 1 and vs[0].verdict == MIGRATED,
          "host names compare without case, so APP-01 is app-01")
    marker = head + "1,DBOLD,app-01,,1,\n" + poll(1, "DBNEW", [])
    marker += poll(2, "DBOLD", []) + poll(2, "DBNEW", [])
    marker += poll(3, "DBOLD", []) + poll(3, "DBNEW", [])
    check(verdicts(marker) == [],
          "a row with a host but no session_id is a marker, not a session")
    ten = head
    for r in range(1, 11):
        o = 3 if r < 9 else 0
        ten += poll(r, "DBOLD", [("app-01", o)] if o else [])
        ten += poll(r, "DBNEW", [] if o else [("app-01", 3)])
    v = one(ten)
    check(v.verdict == MIGRATED and v.first_new == 9,
          "rounds sort as numbers, so round 10 comes after round 9"
          "  <-- pinned defect")
    check(spans([1, 2, 3, 5, 7, 8]) == "1-3, 5, 7-8" and spans([]) == "",
          "round lists print as ranges")
    upper = series([(2, 0), (2, 0), (0, 2), (0, 2)]).replace(
        "round,server,host_name", "ROUND, Server ,HOST_NAME")
    check(one(upper).verdict == MIGRATED,
          "column names match without case or surrounding blanks")
    typed = "#TYPE System.Data.DataRow\n" + series(
        [(2, 0), (2, 0), (0, 2), (0, 2)])
    check(one(typed).verdict == MIGRATED,
          "a Windows PowerShell #TYPE line above the header is skipped"
          "  <-- pinned defect")
    raises(lambda: parse_export("#TYPE System.Data.DataRow"),
           "a file that is only a #TYPE line has no columns", UsageError)
    bad_round = typed.replace("3,DBNEW,app-01,51", "x,DBNEW,app-01,51")
    msg = message(lambda: build_polls(parse_export(bad_round), "DBOLD",
                                      "DBNEW"), BadInput)
    check(msg.startswith("line 10:"),
          "a bad round names its physical line, counting the #TYPE line")
    raises(lambda: parse_export("round,server\n1,DBOLD\n"),
           "a missing column is a usage error naming it", UsageError)
    msg = message(lambda: parse_export(""), UsageError)
    check("(no columns at all)" in msg,
          "an empty export says it has no columns")
    third = head + ("1,DBTHIRD,app-01,51,1,\n1,DBTHIRD,app-01,52,1,\n"
                    "1,DBFOURTH,,,1,\n")
    msg = message(lambda: build_polls(parse_export(third), "DBOLD", "DBNEW"),
                  UsageError)
    check(msg.startswith("server(s) 'DBTHIRD', 'DBFOURTH' are on rows"),
          "a third server is refused, not dropped, and each name prints once")
    no_old = head + poll(1, "DBNEW", [("app-01", 2)])
    msg = message(lambda: build_polls(parse_export(no_old), "DBOLD", "DBNEW"),
                  UsageError)
    check("--old 'DBOLD' is on no row" in msg and "through the alias" in msg,
          "an export with no row for OLD is refused and names the alias trap"
          "  <-- pinned defect")
    raises(lambda: build_polls(parse_export(
        head + poll(1, "DBOLD", [("app-01", 2)])), "DBOLD", "DBNEW"),
        "an export with no row for NEW is refused", UsageError)
    two = head + "1,DBOLD,app-01,51,1,\n1,DBOLD,app-01,51,1,x\n"
    two += "1,DBOLD,app-01,52,1,y\n"
    two += poll(1, "DBNEW", [])
    p, _ = build_polls(parse_export(two), "DBOLD", "DBNEW")
    check(p[1]["old"].time == "x",
          "the first non-blank poll_time of a poll is the one kept")
    short = "round,server,host_name,session_id,view_server_state\n"
    short += "1,DBOLD,app-01,51,1\n1,DBNEW,,,1\n"
    p, _ = build_polls(parse_export(short), "DBOLD", "DBNEW")
    check(p[1]["old"].time == "", "poll_time is optional")
    ragged = head + "1,DBOLD,app-01\n" + poll(1, "DBNEW", [])
    p, _ = build_polls(parse_export(ragged), "DBOLD", "DBNEW")
    check(p[1]["old"].blind and not p[1]["old"].sessions,
          "a short row reads its missing cells as blank, and a blank "
          "view_server_state is blind")
    blank = head + "1,DBOLD,,51,1,\n" + poll(1, "DBNEW", [])
    p, labels = build_polls(parse_export(blank), "DBOLD", "DBNEW")
    check(labels.get("") == "(blank host_name)",
          "a session with a blank host_name is reported, not dropped")

    # ---- --node
    both = head + poll(1, "DBOLD", [("app-01", 2), ("app-02", 2)])
    both += poll(1, "DBNEW", [])
    for r in (2, 3):
        both += poll(r, "DBOLD", [("app-02", 1)])
        both += poll(r, "DBNEW", [("app-01", 2), ("app-02", 1)])
    vs = verdicts(both)
    check([(x.label, x.verdict) for x in vs] ==
          [("app-01", MIGRATED), ("app-02", SPLIT)],
          "each node gets its own verdict")
    check(gate(vs) == 1 and overall(vs).startswith("SPLIT. 1 node"),
          "one SPLIT node fails the run even when another MIGRATED")
    vs = verdicts(both, nodes=["app-01", "APP-01", "app-03"])
    check([(x.label, x.verdict) for x in vs] ==
          [("app-01", MIGRATED), ("app-03", INCONCLUSIVE)],
          "--node keeps the order given and drops a repeat")
    check("never seen in any poll" in vs[1].reason,
          "a --node that is on no row is INCONCLUSIVE, never MIGRATED"
          "  <-- pinned defect")
    check(gate(vs) == 3, "and one unseen node makes the run exit 3")
    check(overall(vs).startswith("NOT CERTIFIED. 1 of 2"),
          "and the run says NOT CERTIFIED")
    check(gate([]) == 3 and overall([]).startswith("INCONCLUSIVE"),
          "no node at all certifies nothing  <-- pinned defect")
    vs = verdicts(series([(4, 0), (4, 0), (4, 0)]).replace(
        "app-01", "app-09"), nodes=None)
    check(overall(vs).startswith("STAYED. 1 node"),
          "the run verdict names STAYED when no node is SPLIT")
    vs = verdicts(series([(4, 0), (4, 0), (0, 4), (0, 4)]))
    check(overall(vs).startswith("MIGRATED. Every node"),
          "the run verdict is MIGRATED only when every node is")
    rows = list(out_rows(vs))
    check(rows[0]["split_rounds"] == "" and rows[0]["first_round_on_new"] == 3
          and rows[0]["final_old"] == 0,
          "the output row carries the counts and the first round on NEW")
    rows = list(out_rows(verdicts(series([(0, 3), (0, 3), (0, 3)]))))
    check(rows[0]["baseline_old"] == 0 and rows[0]["final_old"] == "",
          "an INCONCLUSIVE row leaves the counts it never reached blank")
    rows = list(out_rows(verdicts(series([(0, 3), (0, 3)]))))
    check(rows[0]["baseline_old"] == "",
          "a row refused before the baseline leaves the baseline blank")

    # ---- the preflight
    hosts = ("# Copyright notice and comments\n"
             "127.0.0.1       localhost\n"
             "198.51.100.20   db.example.test   # rehearsal\n")
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["198.51.100.20"], hosts)
    check(ok and lines[-1].startswith("PREFLIGHT PASS"),
          "the resolver answering the new address passes the preflight")
    check("hosts file: line 3 maps db.example.test to 198.51.100.20" in lines,
          "and the hosts line that maps it is named")
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["192.0.2.10"], hosts)
    check(not ok and "on disk and not in effect" in lines[-2],
          "a hosts line on disk while the resolver answers the old address "
          "FAILS  <-- pinned defect")
    check("would test the old address" in lines[-1],
          "and says what a session verdict would then mean")
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["198.51.100.20", "192.0.2.10"])
    check(not ok and "may connect to any of them" in lines[-2],
          "the new address beside the old one fails: a client may pick either"
          "  <-- pinned defect")
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["192.0.2.10", "192.0.2.10"], "")
    check(not ok and lines[0] == "hosts file: no active line names "
          "db.example.test" and "answers 192.0.2.10 for" in lines[1],
          "no hosts line and the old answer fails, and a repeat prints once")
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["198.51.100.20"], "")
    check(ok, "with no hosts line, DNS answering the new address passes")
    commented = "# 198.51.100.20 db.example.test\n"
    commented += "192.0.2.10\tother.example.test DB.Example.Test\n"
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["192.0.2.10"], commented)
    check(lines[0] == "hosts file: line 2 maps db.example.test to 192.0.2.10, "
          "not 198.51.100.20",
          "a commented line is not active, and a second name on a tab line "
          "matches without case")
    check(not ok and "on disk" not in lines[-2],
          "a file that maps the alias elsewhere is not called on disk")
    typo = "198.51.100.2O db.example.test\n"
    ok, lines = preflight("db.example.test", "198.51.100.20",
                          ["192.0.2.10"], typo)
    check("'198.51.100.2O' is not an address" in lines[0],
          "a hosts line whose address has a letter O is named, not trusted")
    check(hosts_lines("198.51.100.20\n", "198.51.100.20") == [],
          "a line with an address and no name maps nothing")
    ok, _ = preflight("db.example.test", "2001:db8::20", ["2001:DB8::20"])
    check(ok, "IPv6 addresses compare as addresses, not as text")
    raises(lambda: preflight("a", "not-an-ip", ["192.0.2.1"]),
           "--expect that is not an address is a usage error", UsageError)
    raises(lambda: preflight("a", "192.0.2.1", ["db.example.test"]),
           "--resolved that is not an address is a usage error", UsageError)

    # ---- the CLI
    tmp = tempfile.mkdtemp(prefix="sessorigin-")

    def tmpfile(name, text, mode="w"):
        path = os.path.join(tmp, name)
        if mode == "wb":
            with open(path, "wb") as handle:
                handle.write(text)
        else:
            with open(path, "w", newline="", encoding="utf-8") as handle:
                handle.write(text)
        return path

    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(argv)
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    split_csv = tmpfile("split.csv", series([(8, 0), (8, 0), (2, 6), (2, 6)]))
    good_csv = tmpfile("good.csv", series([(4, 0), (4, 0), (0, 4), (0, 4)]))
    bom_csv = tmpfile("bom.csv", b"\xef\xbb\xbf" + series(
        [(4, 0), (4, 0), (0, 4), (0, 4)]).encode("ascii"), "wb")
    hosts_txt = tmpfile("hosts.txt", hosts)
    names = ["--old", "DBOLD", "--new", "DBNEW"]

    code, out, err = run_cli([split_csv] + names)
    check(code == 1 and "VERDICT: SPLIT." in out and "MIGRATED" not in out,
          "a split run exits 1 and never prints MIGRATED  <-- pinned defect")
    code, out, err = run_cli([good_csv] + names)
    check(code == 0 and "VERDICT: MIGRATED." in out,
          "a clean run exits 0 and says MIGRATED")
    code, out, err = run_cli([bom_csv] + names)
    check(code == 0, "a UTF-8 BOM before the header is stripped")
    utf16 = tmpfile("u16.csv", series([(4, 0)]).encode("utf-16"), "wb")
    code, out, err = run_cli([utf16] + names)
    check(code == 2 and "UTF-16" in err,
          "a UTF-16 export is refused with exit 2, not misread")
    code, out, err = run_cli([os.path.join(tmp, "absent.csv")] + names)
    check(code == 2 and out == "", "an unreadable export exits 2")
    bad = tmpfile("bad.csv", head + "one,DBOLD,app-01,51,1,\n")
    code, out, err = run_cli([bad] + names)
    check(code == 2 and "not a whole number" in err,
          "a malformed row exits 2")
    huge = tmpfile("huge.csv", head + "1,DBOLD,app-01,\"" + "x" * 140000 +
                   "\",1,\n")
    code, out, err = run_cli([huge] + names)
    check(code == 2, "a field over the csv module's limit exits 2, not a "
          "traceback")
    code, out, err = run_cli([split_csv, "--old", "DBX", "--new", "DBNEW"])
    check(code == 64 and "neither --old" in err,
          "an --old name the export does not hold is a usage error")

    out_csv = os.path.join(tmp, "verdicts.csv")
    code, out, err = run_cli([good_csv] + names + ["--out", out_csv])
    check(code == 0 and not os.path.exists(out_csv) and "not written" in out,
          "--out without --apply writes nothing at all  <-- pinned defect")
    code, out, err = run_cli([good_csv] + names + ["--out", out_csv,
                                                   "--apply"])
    with open(out_csv, newline="", encoding="utf-8") as handle:
        written = list(csv.DictReader(handle))
    check(code == 0 and "wrote " in out and written[0]["verdict"] == MIGRATED
          and list(written[0]) == list(OUT_COLUMNS),
          "--out with --apply writes one row per node")
    code, out, err = run_cli([good_csv] + names + [
        "--out", os.path.join(tmp, "no-such-dir", "v.csv"), "--apply"])
    check(code == 2 and "could not write" in err,
          "a write that fails exits 2, not the 0 of a clean run")

    for prefix in ("--ap", "--appl"):
        code, out, err = run_cli([good_csv] + names + ["--out", out_csv,
                                                       prefix])
        check(code == 2 and "unrecognized arguments: %s" % prefix in err,
              "a prefix %s of --apply is refused by the parser"
              "  <-- pinned defect" % prefix)
    code, out, err = run_cli([good_csv, "--ol", "DBOLD", "--new", "DBNEW"])
    check(code == 2 and "unrecognized" in err,
          "a prefix of any other long flag is refused too")

    usage = [
        ([], "nothing to check"),
        ([good_csv], "needs --old and --new"),
        ([good_csv, "--old", "DBOLD", "--new", "dbold"], "must differ"),
        ([good_csv] + names + ["--settle", "0"], "--settle"),
        ([good_csv] + names + ["--apply"], "--apply needs --out"),
        (["--alias", "a", "--expect", "192.0.2.1", "--resolved", "192.0.2.1",
          "--out", out_csv], "--out needs a session export"),
        (["--alias", "a"], "--alias, --expect and --resolved"),
        (["--hosts-file", hosts_txt], "a preflight needs --alias"),
        ([good_csv] + names + ["--alias", "a", "--expect", "192.0.2.1",
                               "--resolved", "192.0.2.1"],
         "exactly one --node"),
        (["--alias", "a", "--expect", "x", "--resolved", "192.0.2.1"],
         "not an IP address"),
    ]
    for argv, needle in usage:
        code, out, err = run_cli(argv)
        check(code == 64 and needle in err,
              "usage error: %s" % needle)

    pre = ["--alias", "db.example.test", "--expect", "198.51.100.20",
           "--hosts-file", hosts_txt]
    code, out, err = run_cli(pre + ["--resolved", "198.51.100.20"])
    check(code == 0 and "PREFLIGHT PASS" in out and "VERDICT" not in out,
          "a preflight on its own passes with exit 0")
    code, out, err = run_cli(pre + ["--resolved", "192.0.2.10"])
    check(code == 3 and "on disk and not in effect" in out,
          "a failed preflight on its own exits 3")
    os.remove(out_csv)
    code, out, err = run_cli([good_csv] + names + pre + [
        "--resolved", "192.0.2.10", "--node", "app-01", "--out", out_csv,
        "--apply"])
    check(code == 3 and "MIGRATED" not in out and "withheld" in out,
          "a failed preflight withholds every session verdict"
          "  <-- pinned defect")
    check(not os.path.exists(out_csv),
          "and a withheld run writes nothing, even with --apply"
          "  <-- pinned defect")
    code, out, err = run_cli([split_csv] + names + pre + [
        "--resolved", "198.51.100.20", "--node", "app-01"])
    check(code == 1 and "PREFLIGHT PASS" in out and "VERDICT: SPLIT." in out,
          "a passed preflight lets the session verdict through")
    code, out, err = run_cli(pre[:-1] + [os.path.join(tmp, "nohosts.txt"),
                                         "--resolved", "198.51.100.20"])
    check(code == 2 and out == "", "an unreadable hosts file exits 2")
    u16 = tmpfile("hosts16.txt", hosts.encode("utf-16-le"), "wb")
    code, out, err = run_cli(pre[:-1] + [u16, "--resolved", "198.51.100.20"])
    check(code == 2 and "UTF-16" in err,
          "a UTF-16 hosts file is refused, not read as having no line"
          "  <-- pinned defect")

    argv_before = sys.argv
    try:
        sys.argv = ["sessorigin.py", good_csv] + names
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure  <-- pinned defect")
    check(message(lambda: None) == "",
          "message() of a call that raises nothing is empty")
    sys.stdout = io.StringIO()
    try:
        names_seen = runpy.run_path(os.path.abspath(__file__),
                                    run_name="sessorigin_probe")
        printed = sys.stdout.getvalue()
    finally:
        sys.stdout = quiet
    check("main" in names_seen and printed == "",
          "loading the file as a module runs nothing and prints nothing")

    sys.stdout = io.StringIO()
    try:
        code = _summary(5, ["probe label"])
        report = sys.stdout.getvalue()
    finally:
        sys.stdout = quiet
    check(code == 1 and "5 assertions, 1 failed" in report and
          "FAILED: probe label" in report,
          "a run with a failure says so and returns 1")

    shutil.rmtree(tmp, ignore_errors=True)
    return _summary(passed[0] + len(failed), failed)


def _summary(total, failed):
    """The footer of a self-test run, and its exit code."""
    print("-" * 68)
    if failed:
        print("%d assertions, %d failed" % (total, len(failed)))
        for f in failed:
            print("  FAILED: %s" % f)
        return 1
    print("%d assertions, 0 failed" % total)
    return 0


# ----------------------------------------------------------------------- cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="sessorigin.py",
        description="Give a verdict from exported session rows after a "
                    "database alias flip. Never reports MIGRATED in a split.",
        epilog="Nothing is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("sessions", nargs="?", metavar="SESSIONS",
                    help="the exported poll rows, as a CSV")
    ap.add_argument("--old", help="@@SERVERNAME of the server the alias left")
    ap.add_argument("--new", help="@@SERVERNAME of the server it now names")
    ap.add_argument("--node", action="append",
                    help="a client host_name to judge. Repeat for more. "
                         "Default: every host in the export.")
    ap.add_argument("--settle", type=int, default=DEFAULT_SETTLE,
                    help="trailing rounds that must all show the node only "
                         "on NEW (default %d)" % DEFAULT_SETTLE)
    ap.add_argument("--alias", help="preflight: the DNS name that was flipped")
    ap.add_argument("--expect", help="preflight: the address it should "
                                     "resolve to")
    ap.add_argument("--resolved", action="append",
                    help="preflight: an address the node's resolver returned. "
                         "Repeat for each one.")
    ap.add_argument("--hosts-file", dest="hosts_file",
                    help="preflight: a copy of the node's hosts file")
    ap.add_argument("--out", help="path for the per-node verdict CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def _usage(args):
    """The first usage problem with these arguments, or None."""
    pre = [args.alias, args.expect, args.resolved, args.hosts_file]
    if not args.sessions and not any(pre):
        return ("nothing to check: give a session export, a preflight, or "
                "--self-test")
    if any(pre) and not (args.alias and args.expect and args.resolved):
        return "a preflight needs --alias, --expect and --resolved"
    if args.apply and not args.out:
        return "--apply needs --out"
    if args.out and not args.sessions:
        return "--out needs a session export"
    if args.sessions:
        if not args.old or not args.new:
            return "a session export needs --old and --new"
        if args.old.lower() == args.new.lower():
            return "--old and --new must differ"
        if args.settle < 1:
            return "--settle must be 1 or more"
        if any(pre) and len(args.node or []) != 1:
            return ("a preflight speaks for one node: name it with exactly "
                    "one --node")
    return None


def main(argv=None):
    args = _parse(sys.argv[1:] if argv is None else argv)

    if args.self_test:
        return self_test()

    problem = _usage(args)
    if problem:
        print("error: %s" % problem, file=sys.stderr)
        return 64

    try:
        hosts_text = read_text(args.hosts_file) if args.hosts_file else None
        if args.alias:
            passed, lines = preflight(args.alias, args.expect, args.resolved,
                                      hosts_text)
            for line in lines:
                print(line)
            if not args.sessions:
                return 0 if passed else 3
            if not passed:
                print("\nSession verdicts withheld: the node never resolved "
                      "the alias to the new address.")
                return 3
            print("")
        records = parse_export(read_text(args.sessions))
        polls, labels = build_polls(records, args.old, args.new)
    except UsageError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    except (BadInput, IOError, OSError, csv.Error) as exc:
        # csv.Error too: a field over csv's 131072 character limit is an
        # unreadable input, not a verdict.
        print("error: %s" % exc, file=sys.stderr)
        return 2

    verdicts = assess(polls, labels, args.node, args.settle)
    print("sessorigin: %s" % args.sessions)
    for line in describe(verdicts, polls, args.old, args.new, args.settle):
        print(line)

    if args.out:
        if not args.apply:
            print("\nCheck only. %s was not written. Re-run with --apply."
                  % args.out)
        else:
            try:
                with open(args.out, "w", newline="",
                          encoding="utf-8") as handle:
                    writer = csv.DictWriter(handle,
                                            fieldnames=list(OUT_COLUMNS))
                    writer.writeheader()
                    writer.writerows(out_rows(verdicts))
            except (IOError, OSError) as exc:
                print("error: could not write %s: %s" % (args.out, exc),
                      file=sys.stderr)
                return 2
            print("\nwrote %s" % args.out)

    return gate(verdicts)


if __name__ == "__main__":
    sys.exit(main())
