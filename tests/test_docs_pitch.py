"""Plain-text assertions for the judge-facing pitch deck and the demo script.

Requirements covered: R-040 (deck), R-041 (demo script core, extended,
pre-flight, Q&A and fallback ladder), R-062 (backup video shot list), R-063
(the four brief questions in order), R-064 (named Utah user), R-083 (the
pitch's honest-limits and faces lines) and R-100 (battery and rehearsal plan).
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
PITCH = DOCS / "pitch.md"
SCRIPT = DOCS / "demo-script.md"
PLAN = DOCS / "one-page-plan.md"
PRIVACY = DOCS / "privacy-design.md"
BENCHMARK = DOCS / "altitude-benchmark.md"
FLEET_SCALING = DOCS / "fleet-scaling.md"
FLEET_SETUP = DOCS / "fleet-setup.md"

# Brief section 06, verbatim and in this order.
QUESTIONS = (
    "What's the hard tech?",
    "Show it working",
    "Who needs it?",
    "What does $10,000 unlock?",
)

UTAH_USER = "Salt Lake County Sheriff's Search and Rescue Team"
UTAH_USER_URL = "https://saltlakecounty.gov/sheriff/law-enforcement/search--rescue-team"
UTAH_USER_LABEL = "target customer (ASSUMPTION: no contact yet)"


def _section(text: str, heading: str) -> str:
    """Return the body of the level-2 section whose heading starts with `heading`."""
    match = re.search(
        rf"^## {re.escape(heading)}[^\n]*\n(.*?)(?=^## |\Z)", text, re.S | re.M
    )
    assert match, f"missing section: {heading}"
    return match.group(1)


def _assert_in_order(text: str, needles: tuple[str, ...], where: str) -> list[int]:
    positions = []
    for needle in needles:
        assert needle in text, f"{where}: missing {needle!r}"
        positions.append(text.index(needle))
    assert positions == sorted(positions), f"{where}: out of order {list(needles)}"
    return positions


def _table_rows(section: str) -> list[list[str]]:
    """Return every Markdown table row of a section, header first, separators dropped."""
    rows = []
    for line in section.splitlines():
        if line.startswith("|") and not re.match(r"^\|[-:| ]+\|$", line):
            rows.append([cell.strip() for cell in line.strip().strip("|").split("|")])
    return rows


def _core(text: str) -> str:
    return _section(text, "Core (4:00)")


# R-040: the deck.


def test_pitch_has_at_most_five_slides():
    text = PITCH.read_text()
    headings = re.findall(r"^## (.+)$", text, re.M)
    slides = [h for h in headings if re.match(r"Slide \d+\b", h)]
    assert 4 <= len(slides) <= 5, slides
    # Every level-2 heading is a slide, so no extra slide hides under another name.
    assert len(slides) == len(headings), headings
    numbers = [int(re.match(r"Slide (\d+)", h).group(1)) for h in slides]
    assert numbers == list(range(1, len(slides) + 1))


def test_pitch_covers_required_topics():
    text = PITCH.read_text()
    for needle in ("Problem", "Solution", "Built this week", "Utah", "$10,000", "$5,000",
                   "altitude_recall.png"):
        assert needle in text, needle
    # The benchmark plot is embedded as an image and exists on disk.
    assert re.search(r"!\[[^\]]*\]\(img/altitude_recall\.png\)", text)
    assert (DOCS / "img" / "altitude_recall.png").is_file()
    # What existed before kickoff and where the week's delta is listed.
    for needle in ("798f21a", "hackathon/kickoff", "built-this-week.md"):
        assert needle in text, needle


def test_every_stat_line_cited_or_assumption():
    offenders = [
        line for line in PITCH.read_text().splitlines()
        if re.search(r"\d{2,}", line) and "http" not in line and "ASSUMPTION" not in line
    ]
    assert not offenders, offenders


def test_pitch_numbers_match_merged_docs():
    pitch = PITCH.read_text()
    benchmark = BENCHMARK.read_text()
    for needle in ("0.142", "0.602", "0.832", "0/337", "0.52 to 0.70", "96.8", "0.398",
                   "57.555", "INFEASIBLE"):
        assert needle in pitch, needle
        assert needle in benchmark, needle
    assert "0.7 s" in pitch and "0.7s after the kill" in FLEET_SCALING.read_text()


# R-063: the four questions, verbatim and in order.


def test_four_questions_in_order_in_pitch():
    _assert_in_order(PITCH.read_text(), QUESTIONS, "pitch")


def test_four_questions_in_order_in_demo_script():
    _assert_in_order(_core(SCRIPT.read_text()), QUESTIONS, "demo script core")


# R-064: the named Utah user, in the "Who needs it?" part of both docs.


def test_named_utah_user_with_url():
    for name, text in (("pitch", PITCH.read_text()), ("demo script core", _core(SCRIPT.read_text()))):
        start = text.index(QUESTIONS[2])
        end = text.index(QUESTIONS[3])
        who = text[start:end]
        for needle in (UTAH_USER, UTAH_USER_URL, UTAH_USER_LABEL):
            assert needle in who, f"{name}: {needle}"


# R-083: honest limits and faces (the pitch side; the plan and privacy docs are checked too).


def test_honest_limits_and_faces_lines():
    for path in (PITCH, PLAN):
        lines = path.read_text().splitlines()
        limit = [ln for ln in lines if "canopy" in ln and "open terrain" in ln and "thermal" in ln]
        assert limit, f"{path.name}: no canopy / open terrain sentence"
        for terrain in ("desert", "canyon", "meadow", "snow"):
            assert terrain in limit[0], f"{path.name}: {terrain}"
    for path in (PITCH, PRIVACY):
        assert "not resolvable" in path.read_text(), path.name


# R-041: core, extended, pre-flight, fallback ladder and Q&A.


def test_demo_script_core_and_extended():
    text = SCRIPT.read_text()
    for needle in ("Core (4:00)", "Extended", "0:00", "4:00", "798f21a", "hackathon/kickoff",
                   "docs/built-this-week.md", "FLIGHTRISK_FLEET_NET", "station", "ap"):
        assert needle in text, needle

    # Core blocks are back-to-back time ranges from 0:00 to 4:00, and every cue
    # time inside a block falls within that block's range.
    blocks = re.split(r"^### ", _core(text), flags=re.M)[1:]
    ranges = []
    for block in blocks:
        heading, _, body = block.partition("\n")
        m = re.match(r"(\d+):(\d{2})[–-](\d+):(\d{2})\b", heading)
        assert m, f"core block heading without a time range: {heading}"
        start, end = int(m[1]) * 60 + int(m[2]), int(m[3]) * 60 + int(m[4])
        ranges.append((start, end))
        for mins, secs in re.findall(r"\b(\d+):(\d{2})\b", body):
            assert start <= int(mins) * 60 + int(secs) <= end, f"{heading}: cue {mins}:{secs}"
    assert ranges and ranges[0][0] == 0 and ranges[-1][1] == 240, ranges
    for (_, prev_end), (start, _) in zip(ranges, ranges[1:]):
        assert start == prev_end, ranges

    # Extended blocks are numbered in order and the slot lengths are covered.
    extended = _section(text, "Extended")
    blocks = [int(n) for n in re.findall(r"^### E(\d+)\b", extended, re.M)]
    assert len(blocks) >= 3 and blocks == list(range(1, len(blocks) + 1)), blocks
    for slot in ("8 min", "25 min"):
        assert slot in extended, slot


def test_demo_script_preflight_covers_both_paths():
    preflight = _section(SCRIPT.read_text(), "Pre-flight checklist")
    for needle in ("scripts/tello_hw_check.py", "FLIGHTRISK_FLEET_NET=station",
                   "FLIGHTRISK_FLEET_NET=ap", "FLIGHTRISK_EDGE_TOKEN", "30%", "floor"):
        assert needle in preflight, needle


def test_launch_commands_match_fleet_setup():
    launch = _section(FLEET_SETUP.read_text(), "Launch")
    commands = []
    for block in re.findall(r"```\n(.*?)```", launch, re.S):
        commands += [line.strip() for line in block.splitlines() if line.strip()]
    assert len(commands) == 3, commands
    preflight = _section(SCRIPT.read_text(), "Pre-flight checklist")
    for command in commands:
        assert command in preflight, command


def test_demo_script_has_fallback_ladder():
    text = SCRIPT.read_text()
    assert "Fallback ladder" in text
    ladder = _section(text, "Fallback ladder")
    for needle in ("--source fake", "fleet.mp4", "SITL"):
        assert needle in ladder, needle
    rungs = re.split(r"^(?=\d\. )", ladder, flags=re.M)[1:]
    assert [r[0] for r in rungs] == ["1", "2", "3", "4"], [r[:20] for r in rungs]
    assert "Two Tellos live" in rungs[0]
    assert "--source fake" in rungs[1]
    assert "demo/fleet.mp4" in rungs[2]
    assert "demo_fake_fleet.py --n 4" in rungs[3]
    assert "docs/sitl-demo.md" in rungs[3]


def test_demo_script_has_qa_prep():
    qa = _section(SCRIPT.read_text(), "Q&A prep")
    lines = qa.splitlines()
    questions = [ln for ln in lines if ln.startswith("Q:")]
    answers = [ln for ln in lines if ln.startswith("A:")]
    assert len(questions) >= 5, questions
    assert len(answers) == len(questions)
    for needle in ("canopy", "0.602", "face", "FAA", "AGPL-3.0", "GPL-3.0-or-later"):
        assert needle in qa, needle


# R-062: backup video shot list.


def test_backup_video_shot_list_under_120s():
    shots = _section(SCRIPT.read_text(), "Backup video shot list")
    rows = _table_rows(shots)
    header, body = rows[0], rows[1:]
    col = header.index("Seconds")
    seconds = [int(row[col]) for row in body]
    assert len(seconds) >= 5, body
    assert sum(seconds) <= 120, sum(seconds)
    text = " ".join(" ".join(row) for row in body).lower()
    for needle in ("plot", "skew", "merged map", "re-task", "$5,000", "$10,000"):
        assert needle in text, needle


# R-100: battery and rehearsal plan.


def test_battery_plan():
    plan = _section(SCRIPT.read_text(), "Battery and rehearsal plan")
    for needle in ("13 min", "ryzerobotics.com/tello/specs", "6-minute", "spare", "ASSUMPTION",
                   "rehearsal", "Rehearsal 1", "Rehearsal 2"):
        assert needle in plan, needle
