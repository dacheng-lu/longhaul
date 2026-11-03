"""Release-contract tests for v1.2: platform scope, version identity, CI capability.

The checkpoint CLI needs descriptor-anchored path traversal (openat with
O_NOFOLLOW, fstat, os.replace with directory descriptors) and the eval harness needs
bwrap, so v1.2 is Linux-only. These tests pin the CI matrix and its bubblewrap probe,
the release version in SKILL.md, README.md and the dated CHANGELOG heading, and the
Linux-only statement in every language section. They are plain text/regex checks
because the repository has no YAML dependency and must stay standard-library only.
"""
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CI = REPO / '.github' / 'workflows' / 'ci.yml'
SKILL = REPO / 'SKILL.md'
README = REPO / 'README.md'
CHANGELOG = REPO / 'CHANGELOG.md'

RELEASE = '1.2'
RELEASE_DATE = '2026-11-01'

# The checkout must keep the full history, and the check must read the setting itself:
# a commented-out `# fetch-depth: 0` satisfied the old substring test (open finding v1.2.1).
FETCH_DEPTH_LINE = re.compile(r'^\s+fetch-depth:\s*0\s*$', re.M)


def changelog_section(text):
    """The 1.2 section only, without the older releases."""
    assert '## ' + RELEASE in text, 'no 1.2 section'
    return text.split('## ' + RELEASE, 1)[1].split('\n## 1.1', 1)[0]


def readme_sections(text):
    """The en/zh/de sections, split at their anchors."""
    anchors = ('<a name="zh-cn"></a>', '## Deutsch', '## Files and design references')
    for anchor in anchors:
        assert anchor in text, 'README anchor missing: %s' % anchor
    zh = text.index(anchors[0])
    de = text.index(anchors[1])
    end = text.index(anchors[2])
    return {'en': text[:zh], 'zh': text[zh:de], 'de': text[de:end]}


class TestReleaseIdentity:
    """REL1: SKILL, README and the CHANGELOG heading name the same release."""

    def test_skill_md_declares_the_release_version(self):
        text = SKILL.read_text()
        assert re.search(r'(?m)^Version: %s\s*$' % re.escape(RELEASE), text), \
            [line for line in text.splitlines() if line.startswith('Version:')]

    def test_readme_declares_the_release_version(self):
        text = README.read_text()
        assert 'Skill version: **%s**' % RELEASE in text, \
            [line for line in text.splitlines() if 'Skill version' in line]

    def test_changelog_opens_with_a_dated_release_heading(self):
        text = CHANGELOG.read_text()
        heading = r'(?m)^## %s — %s\s*$' % (re.escape(RELEASE), re.escape(RELEASE_DATE))
        assert re.search(heading, text), [line for line in text.splitlines()
                                          if line.startswith('## ')]

    def test_changelog_no_longer_claims_an_unchanged_skill(self):
        section = changelog_section(CHANGELOG.read_text())
        assert 'unchanged at 1.1' not in section, section[:300]

    def test_historical_eval_conditions_are_unchanged(self):
        """v1 and v1.1 are measurement conditions, not the release version."""
        section = changelog_section(CHANGELOG.read_text())
        assert '`v1` at b8b7d8c' in section, section[:400]
        assert '`v1.1` at af52cd8' in section, section[:400]


class TestCiMatrix:
    """R-P2: the workflow matrix is ubuntu-latest only, with the reason written down."""

    def test_the_workflow_runs_on_ubuntu_only(self):
        text = CI.read_text()
        lines = [line.strip() for line in text.splitlines()
                 if line.strip().startswith('os:')]
        assert len(lines) == 1, lines
        assert 'ubuntu-latest' in lines[0], lines
        assert 'macos' not in lines[0].lower(), lines
        assert 'windows' not in lines[0].lower(), lines

    def test_no_other_operating_system_is_smuggled_into_the_matrix(self):
        """Comments may explain the exclusion; a matrix value may not name another OS."""
        text = CI.read_text()
        assert 'matrix:' in text, text
        matrix = text.split('matrix:', 1)[1].split('steps:', 1)[0]
        entries = '\n'.join(line for line in matrix.splitlines()
                            if not line.strip().startswith('#'))
        assert 'macos' not in entries.lower(), entries
        assert 'windows' not in entries.lower(), entries

    def test_the_checkout_fetches_the_full_history(self):
        """The eval harness builds its pinned conditions with `git archive <sha>` (v1 at b8b7d8c, v1.1 at
        af52cd8), so a shallow checkout fails 11 end-to-end tests (10 of them with 'not a valid object name')
        and skips 2 pin tests in test_eval_runner.py. The first CI run found this: it ran with the
        default depth of 1."""
        text = CI.read_text()
        step = text.split('actions/checkout@v4', 1)[1].split('- uses:', 1)[0].split('- name:', 1)[0]
        assert FETCH_DEPTH_LINE.search(step), step

    def test_a_commented_out_fetch_depth_does_not_satisfy_the_checkout_rule(self):
        """The old assertion was `'fetch-depth: 0' in step`, which a commented-out line satisfies.

        Open finding v1.2.1. The check now reads the line itself, so commenting the setting out
        must fail it even though the string is still present in the step.
        """
        text = CI.read_text()
        step = text.split('actions/checkout@v4', 1)[1].split('- uses:', 1)[0].split('- name:', 1)[0]
        commented = '\n'.join('# ' + line if 'fetch-depth' in line else line for line in step.splitlines())
        assert 'fetch-depth: 0' in commented, commented  # what the old assertion accepted
        assert not FETCH_DEPTH_LINE.search(commented), commented  # what the check must reject

    def test_the_gate_tests_keep_pytests_temp_dir_off_tmp(self):
        """The eval harness masks /tmp (and /var/tmp) inside its sandbox and counts whatever it finds there
        as host contamination, and the fake hermes entry of the tests lives under pytest's temporary
        directory. pytest's default base is /tmp/pytest-of-<user>, so on a runner with no TMPDIR six tests
        failed with 'baseline: /tmp carries 1 host entry/entries (pytest-of-runner)'. The remedy is pytest's
        documented PYTEST_DEBUG_TEMPROOT, pointed at the runner's own temp directory (pyinstaller's CI does
        the same). Matched per line so that a comment or another step cannot satisfy it."""
        text = CI.read_text()
        step = text.split('- name: gate tests', 1)[1]
        assert re.search(r'^\s+PYTEST_DEBUG_TEMPROOT:\s*\$\{\{\s*runner\.temp\s*\}\}\s*$', step, re.M), step
        assert re.search(r'^\s+run:\s*python -m pytest tests -q\s*$', step, re.M), step

    def test_the_workflow_states_why(self):
        comments = [line.strip() for line in CI.read_text().splitlines()
                    if line.strip().startswith('#')]
        assert any('linux' in line.lower() and 'descriptor' in line.lower()
                   for line in comments), comments

    def test_the_workflow_installs_and_proves_bubblewrap(self):
        """CI1: bwrap is a hard prerequisite; the workflow must install and probe it."""
        text = CI.read_text()
        assert 'bubblewrap' in text.lower(), [line for line in text.splitlines()
                                              if 'apt-get' in line]
        assert re.search(r'bwrap\s+--ro-bind\s+/\s+/\s+true', text), \
            [line for line in text.splitlines() if 'bwrap' in line]
        assert 'continue-on-error' not in text, 'the bwrap probe must not be softened'
        assert re.search(r'(::error::|exit 1)', text), 'the probe needs a hard failure'


class TestDocumentedScope:
    """R-P2/DOC2: every language section and the CHANGELOG name the Linux-only scope."""

    def test_every_language_section_states_the_linux_only_scope(self):
        """DOC2: one English sentence is not enough; zh and de must say it too."""
        sections = readme_sections(README.read_text())
        for name, body in sections.items():
            assert re.search(r'Linux[ -]only', body), (name, body[-300:])
            assert 'descriptor' in body.lower(), (name, body[-300:])
            assert RELEASE in body, (name, 'no release version in this section')

    def test_changelog_states_the_platform_narrowing(self):
        section = changelog_section(CHANGELOG.read_text())
        assert re.search(r'Linux[ -]only', section), section[-400:]
        assert 'descriptor' in section.lower(), section[-400:]
