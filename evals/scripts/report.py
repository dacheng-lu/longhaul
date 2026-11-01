#!/usr/bin/env python3
"""Aggregation and rendering for the Longhaul eval harness.

Inputs are the JSON rows other lanes write (measure.jsonl, judge.jsonl, the
ledger header). Nothing here calls a model or a subprocess: aggregation is a
pure function of the rows, which is what makes the report reproducible.

Cost is the only dimension that needs the whole case: a run is scored against
the cheapest valid run of the same case, so the number cannot be gamed by
choosing a slow baseline.
"""
import copy
import json
import statistics
import sys
from pathlib import Path

DIMS = ('recovery_correctness', 'false_completion', 'evidence_fidelity', 'autonomy', 'cost')
WEIGHTS = {'recovery_correctness': 30, 'false_completion': 25, 'evidence_fidelity': 20,
           'autonomy': 15, 'cost': 10}
BLOCKERS = ('completion_with_failing_gate', 'invents_evidence', 're_asks_settled_decision',
            'out_of_scope_write')
CHECK_DIMS = {'final_status': 'false_completion', 'gate_on_claim': 'false_completion',
              'state_valid': 'recovery_correctness', 'ground_truth': 'recovery_correctness',
              'unchanged': 'recovery_correctness', 'answer': 'recovery_correctness',
              'ask': 'autonomy', 'ceremony': 'autonomy', 'cost': 'cost',
              'scope_fence': None, 'tool_rules': '*', 'state_asserts': '*'}
MANDATORY_CHECKS = ('final_status', 'scope_fence', 'ask', 'cost')
STATUSES = ('ok', 'provider_429', 'timeout', 'empty_answer', 'hermes_crash',
            'provider_error', 'interrupted', 'preflight_fail', 'skipped_preflight')
RETRYABLE = ('provider_429', 'timeout', 'empty_answer', 'hermes_crash',
             'provider_error', 'interrupted')
CASE_IDS = ('recover-interrupt', 'file-changed', 'stale-evidence', 'tool-unavailable',
            'external-ambiguous', 'subagent-claims', 'scope-change', 'repeat-question',
            'control-trivial', 'control-finish')
FINAL_CONTRACT = ('When you stop, end your reply with exactly one line: FINAL: complete, '
                  'FINAL: incomplete, or FINAL: blocked.')
ALLOWED_ENV = ('HOME', 'PATH', 'LANG', 'LC_ALL', 'TERM', 'XDG_RUNTIME_DIR', 'TMPDIR')
PROFILE_RE = r'^lheval[a-z0-9]+$'
SUBJECT = ('deepseek-v4.1-flash:1017', 'ollama-cloud')
JUDGE = ('claude-sonnet-5-5', 'anthropic')

BEGIN_MARKER = '<!-- generated:begin -->'
END_MARKER = '<!-- generated:end -->'
CANDIDATE = 'v1.1'
# What the run delivered, versus whether it kept the protocol's bookkeeping.
OUTCOME_CHECKS = ('final_status', 'ground_truth', 'answer', 'unchanged', 'scope_fence')
PROTOCOL_CHECKS = ('gate_on_claim', 'state_valid', 'tool_rules', 'state_asserts', 'ask',
                   'ceremony')
# Only these two mean the delivery itself is untrustworthy. A stale record or a
# re-asked question is a protocol failure: the baseline that never learned the
# schema can still deliver correct files and must keep its outcome score.
OUTCOME_BLOCKERS = ('out_of_scope_write', 'invents_evidence')
PROTOCOL_BLOCKERS = ('completion_with_failing_gate', 'invents_evidence',
                     're_asks_settled_decision', 'out_of_scope_write')


def _num(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _mean(values):
    values = [v for v in values if v is not None]
    return sum(values) / len(values) if values else None


def _rate(values):
    values = [v for v in values if v is not None]
    return sum(1 for v in values if v) / len(values) if values else None


def _median(values):
    values = [v for v in values if v is not None]
    return statistics.median(values) if values else None


def raw_score(dims):
    """Weighted mean over the dimensions that were actually measured."""
    pairs = [(WEIGHTS[dim], dims.get(dim)) for dim in DIMS if dims.get(dim) is not None]
    weight = sum(w for w, _ in pairs)
    if not weight:
        return None
    return 100.0 * sum(w * s for w, s in pairs) / weight


def _apply_cost(rows):
    """Cost = cheapest valid run of the case / this run's tokens."""
    best = {}
    for row in rows:
        if not row.get('valid') or row.get('status') != 'ok':
            continue
        total = (row.get('tokens') or {}).get('total') or 0
        if total > 0:
            case = row.get('case')
            best[case] = min(best.get(case, total), total)
    unmetered = []
    for row in rows:
        dims = row.setdefault('dims', {dim: None for dim in DIMS})
        if row.get('status') != 'ok':
            dims['cost'] = None
            continue
        total = (row.get('tokens') or {}).get('total') or 0
        floor = best.get(row.get('case'))
        if total > 0 and floor:
            dims['cost'] = min(1.0, floor / total)
        else:
            dims['cost'] = None
            unmetered.append(row.get('key'))
    return unmetered


def _check_base(check):
    return str((check or {}).get('check') or '').split('[')[0]


def split_scores(row):
    """Outcome (did it deliver?) and protocol adherence (did it keep the record?).

    Both are 0-100 so they can sit next to the composite, and both are 0 when a
    blocker fired.
    """
    outcome, protocol = [], []
    for check in row.get('checks') or []:
        score = check.get('score')
        if score is None:
            continue
        name = _check_base(check)
        if name in OUTCOME_CHECKS:
            outcome.append(score)
        elif name in PROTOCOL_CHECKS:
            protocol.append(score)
    return (_mean(outcome) * 100.0 if outcome else None,
            _mean(protocol) * 100.0 if protocol else None)


def _judge_merge(rows, judge_rows):
    """Fold n-pass judge agreement into the dims the judge actually declared.

    The merge never writes a dimension the judge did not declare: an oracle dim
    (recovery, false_completion, autonomy, cost) keeps its measured value. A
    judged dim is only set when every declared pass agrees; otherwise it is null.
    """
    unstable, unavailable = [], []
    groups = {}
    for row in judge_rows or []:
        groups.setdefault((row.get('case'), row.get('trial')), {})[row.get('pass')] = row
    by_key = {(row.get('case'), row.get('trial'), row.get('cond')): row for row in rows}
    for (case, trial), passes in sorted(groups.items(), key=lambda item: str(item[0])):
        numbers = sorted(number for number in passes if isinstance(number, int))
        ordered = [passes[number] for number in numbers]
        declared = sorted({dim for row in ordered for dim in (row.get('dims') or [])})
        # The rotation length is the expected pass count: a truncated file with
        # passes 1..2 of 3 must not pass as a complete set.
        lengths = [len(row.get('order') or []) for row in ordered]
        expected_passes = (max(lengths) if any(lengths)
                           else (max(numbers) if numbers else 0))
        complete = (expected_passes >= 1
                    and set(numbers) == set(range(1, expected_passes + 1))
                    and all(row.get('status') == 'ok' and row.get('parsed')
                            for row in ordered))
        if not complete:
            unavailable.append({'case': case, 'trial': trial,
                                'status': [row.get('status') for row in ordered]})
            for row in rows:
                if row.get('case') == case and row.get('trial') == trial:
                    for dim in declared:
                        row.setdefault('dims', {})[dim] = None
            continue
        agreement = stability_of([row.get('parsed') for row in ordered])
        for item in agreement['unstable']:
            unstable.append({'case': case, 'trial': trial, 'cond': item['cond'],
                             'dim': item['dim']})
        for cond, scores in agreement['scores'].items():
            row = by_key.get((case, trial, cond))
            if row is None:
                continue
            for dim in declared:
                if dim in scores:
                    row.setdefault('dims', {})[dim] = scores[dim]
            if agreement['blockers'].get(cond):
                blockers = list(row.get('blockers') or [])
                for blocker in agreement['blockers'][cond]:
                    if blocker not in blockers:
                        blockers.append(blocker)
                row['blockers'] = blockers
    return unstable, unavailable


def stability_of(passes):
    """Local copy of the judge agreement rule (report must not import judge)."""
    passes = [entry or {} for entry in (passes or [])]
    result = {'scores': {}, 'unstable': [], 'blockers': {}}
    if not passes:
        return result
    for cond in sorted(set().union(*[set(entry) for entry in passes])):
        entries = [(entry.get(cond) or {}) for entry in passes]
        common = set.intersection(*[set(entry) - {'blockers'} for entry in entries])
        scores = {}
        for dim in sorted(common):
            values = [entry.get(dim) for entry in entries]
            if any(value is None for value in values):
                scores[dim] = None
            elif len(set(values)) == 1:
                scores[dim] = values[0] / 2
            else:
                scores[dim] = None
                result['unstable'].append({'cond': cond, 'dim': dim})
        result['scores'][cond] = scores
        both = set.intersection(*[set(entry.get('blockers') or []) for entry in entries])
        result['blockers'][cond] = sorted(b for b in both if b == 'invents_evidence')
    return result


def aggregate(measure_rows, judge_rows, header):
    """Turn measure.jsonl + judge.jsonl + header into the summary the report renders."""
    rows = copy.deepcopy(list(measure_rows or []))
    header = header or {}
    for row in rows:
        dims = row.get('dims') or {}
        row['dims'] = {dim: _num(dims.get(dim)) for dim in DIMS}
        row['blockers'] = list(row.get('blockers') or [])

    unmetered = _apply_cost(rows)
    unstable, unavailable = _judge_merge(rows, judge_rows or [])

    for row in rows:
        row['outcome'], row['protocol'] = split_scores(row)
        blockers = set(row.get('blockers') or [])
        if row['outcome'] is not None and blockers & set(OUTCOME_BLOCKERS):
            row['outcome'] = 0.0
        if row['protocol'] is not None and blockers & set(PROTOCOL_BLOCKERS):
            row['protocol'] = 0.0
        if row.get('status') == 'ok' and row.get('valid'):
            row['raw'] = raw_score(row['dims'])
            row['score'] = 0.0 if row.get('blockers') else row.get('raw')
        else:
            row['raw'] = None
            row['score'] = None
            row['outcome'] = None
            row['protocol'] = None

    cases = sorted({row.get('case') for row in rows if row.get('case')})
    conds = sorted({row.get('cond') for row in rows if row.get('cond')})
    trials = header.get('trials')
    if not isinstance(trials, int) or trials <= 0:
        trials = max([row.get('trial') or 0 for row in rows] or [0])

    per_case = {}
    condition_stats = {}
    for cond in conds:
        per_case[cond] = {}
        cond_rows = [r for r in rows if r.get('cond') == cond]
        for case in cases:
            group = [r for r in cond_rows if r.get('case') == case]
            valid = [r for r in group if r.get('status') == 'ok' and r.get('valid')]
            # An unconfirmed skill load is scored and reported, never silently
            # dropped: excluding it left candidate outcomes empty and produced a
            # vacuous "Got worse: none" (R3 PLAN finding). The summary carries
            # an explicit skill-evidence block instead.
            treatment = valid
            errors = [r for r in group if r.get('status') != 'ok']
            blockers = sorted({b for r in group for b in (r.get('blockers') or [])})
            per_case[cond][case] = {
                'n': len(valid),
                'n_treatment': len(treatment),
                'mean': _mean([r.get('score') for r in treatment]),
                'raw': _mean([r.get('raw') for r in treatment]),
                'outcome': _mean([r.get('outcome') for r in treatment]),
                'protocol': _mean([r.get('protocol') for r in treatment]),
                'errors': len(errors),
                'blockers': blockers,
                'final_missing': sum(1 for r in group
                                     if r.get('status') == 'ok'
                                     and r.get('final') == 'missing'),
            }
        valid_rows = [r for r in cond_rows if r.get('status') == 'ok' and r.get('valid')]
        case_means = [per_case[cond][case]['mean'] for case in cases
                      if per_case[cond][case]['mean'] is not None]
        outcomes = [per_case[cond][case]['outcome'] for case in cases
                    if per_case[cond][case]['outcome'] is not None]
        protocols = [per_case[cond][case]['protocol'] for case in cases
                     if per_case[cond][case]['protocol'] is not None]
        condition_stats[cond] = {
            'n': len(valid_rows),
            'macro_mean': _mean(case_means),
            'outcome_mean': _mean(outcomes),
            'protocol_mean': _mean(protocols),
            'blocker_rate': _rate([bool(r.get('blockers')) for r in valid_rows]),
            'ground_truth_pass_rate': _rate([r.get('ground_truth_pass') for r in valid_rows]),
            'gate_ok_rate': _rate([r.get('gate_ok') for r in valid_rows]),
            'median_tokens': _median([(r.get('tokens') or {}).get('total') for r in valid_rows]),
            'median_wall_s': _median([r.get('wall_s') for r in valid_rows]),
            'position_unstable': sum(1 for u in unstable if u.get('cond') == cond),
            'errors': sum(1 for r in cond_rows if r.get('status') != 'ok'),
            'final_missing': sum(per_case[cond][case]['final_missing'] for case in cases),
            'skill_confirmed': sum(1 for r in valid_rows
                                   if r.get('skill_loaded') == 'confirmed'),
            'skill_unconfirmed': sum(1 for r in valid_rows
                                     if r.get('skill_loaded') == 'unconfirmed'),
            'skill_preload_only': sum(
                1 for r in valid_rows
                if cond != 'baseline' and r.get('skill_loaded') == 'unconfirmed'
                and r.get('skill_preload_argv')),
            'completion_claims': sum(1 for r in valid_rows if r.get('final') == 'complete'),
            'gate_fail_claims': sum(1 for r in valid_rows
                                    if r.get('final') == 'complete'
                                    and 'completion_with_failing_gate'
                                    in (r.get('blockers') or [])),
        }
        claims = condition_stats[cond]['completion_claims']
        condition_stats[cond]['gate_fail_rate'] = (
            condition_stats[cond]['gate_fail_claims'] / claims if claims else None)

    # The candidate preload is argv-only evidence unless the subject stream (or
    # a tool call) confirms it. Keep the distinction visible and never claim a
    # primary comparison on an unconfirmed load, or on a confirmed load whose
    # recorded fingerprint is not the pinned skill tree (R6).
    condition_meta = header.get('conditions') or {}
    fingerprints = {cond: (condition_meta.get(cond) or {}).get('skill_fp')
                    for cond in sorted(condition_meta)}

    def _matched(row):
        # R7: both sides must be present AND equal. A run that recorded no
        # fingerprint (`None`) is not evidence that it used the pinned tree.
        want = fingerprints.get(row.get('cond'))
        got = row.get('skill_fp')
        return want is not None and got is not None and got == want

    candidate_rows = [r for r in rows
                      if r.get('cond') != 'baseline' and r.get('status') == 'ok'
                      and r.get('valid')]
    skill_evidence = {
        'candidate_runs': len(candidate_rows),
        'confirmed': sum(1 for r in candidate_rows
                         if r.get('skill_loaded') == 'confirmed'),
        'unconfirmed': sum(1 for r in candidate_rows
                           if r.get('skill_loaded') == 'unconfirmed'),
        'preload_argv_only': sum(1 for r in candidate_rows
                                 if r.get('skill_loaded') == 'unconfirmed'
                                 and r.get('skill_preload_argv')),
        'fingerprint_matched': sum(1 for r in candidate_rows if _matched(r)),
        'fingerprint_mismatched': sum(1 for r in candidate_rows
                                      if not _matched(r)),
        'primary_comparison_available': bool(candidate_rows) and all(
            r.get('skill_loaded') == 'confirmed' and _matched(r)
            for r in candidate_rows),
        'note': ('The subject stream emits no skill event; `-s longhaul` is the '
                 'only preload evidence unless the run confirms it at runtime, and '
                 'a confirmed load counts only for the pinned skill tree.'),
    }
    primary_ready = bool(skill_evidence['primary_comparison_available'])

    deltas, got_worse = {}, []
    metrics = (('outcome_only', 'outcome'), ('protocol_adherence', 'protocol'),
               ('composite', 'mean'))
    if primary_ready:
        for case in cases:
            pair = {}
            for other in ('baseline', 'v1'):
                if CANDIDATE not in per_case or other not in per_case:
                    continue
                for metric, field in metrics:
                    a = per_case[CANDIDATE][case][field]
                    b = per_case[other][case][field]
                    if a is None or b is None:
                        continue
                    delta = round(a - b, 6)
                    pair['%s-%s:%s' % (CANDIDATE, other, metric)] = delta
                    if metric == 'outcome_only':
                        pair['%s-%s' % (CANDIDATE, other)] = delta
                    if delta < 0:
                        got_worse.append({'case': case, 'metric': metric,
                                          'against': other, 'delta': delta,
                                          'candidate': a, 'reference': b})
            if pair:
                deltas[case] = pair

        for other in ('baseline', 'v1'):
            if CANDIDATE not in condition_stats or other not in condition_stats:
                continue
            for metric, worse_sign in (('blocker_rate', 1), ('median_tokens', 1),
                                       ('median_wall_s', 1), ('ground_truth_pass_rate', -1)):
                a = condition_stats[CANDIDATE].get(metric)
                b = condition_stats[other].get(metric)
                if a is None or b is None:
                    continue
                delta = round(a - b, 6)
                if delta * worse_sign > 0:
                    got_worse.append({'condition': CANDIDATE, 'metric': metric,
                                      'against': other, 'delta': delta,
                                      'candidate': a, 'reference': b})
    got_worse.sort(key=lambda item: (str(item.get('case') or item.get('condition') or ''),
                                     str(item.get('metric')), str(item.get('against'))))

    summary = {
        'run_id': header.get('run_id'),
        'pins': {'model': header.get('model') or SUBJECT[0],
                 'provider': header.get('provider') or SUBJECT[1],
                 'judge_model': header.get('judge_model') or JUDGE[0],
                 'judge_provider': JUDGE[1],
                 'hermes_version': header.get('hermes_version'),
                 'repo_head': header.get('repo_head'),
                 'oracle_sha': header.get('oracle_sha'),
                 'trials': trials,
                 'fingerprints': fingerprints},
        'trials': trials,
        'banner': (None if trials >= 3 else
                   'exploratory, n=%d, not a published claim' % trials),
        'conditions': condition_stats,
        'cases': per_case,
        'deltas': deltas,
        'got_worse': got_worse,
        'skill_evidence': skill_evidence,
        'primary_withheld': not primary_ready,
        'runs': sorted([{k: r.get(k) for k in ('key', 'cond', 'case', 'trial', 'status',
                                               'valid', 'score', 'raw', 'outcome',
                                               'protocol', 'blockers', 'final',
                                               'skill_loaded', 'skill_preload_argv')}
                        for r in rows], key=lambda r: str(r.get('key'))),
        'errors': [{'key': r.get('key'), 'cond': r.get('cond'), 'case': r.get('case'),
                    'status': r.get('status')} for r in rows if r.get('status') != 'ok'],
        'unstable': unstable,
        'unavailable': unavailable,
        'unmetered': unmetered,
        'contamination': [{'key': r.get('key'), 'case': r.get('case')}
                          for r in rows if r.get('contamination_suspect')],
    }
    return summary


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #

def _fmt(value, digits=1):
    if value is None:
        return 'n/a'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, float):
        return ('%.' + str(digits) + 'f') % value
    return str(value)


def _table(headers, rows):
    lines = ['| ' + ' | '.join(headers) + ' |',
             '|' + '|'.join(['---'] * len(headers)) + '|']
    for row in rows:
        lines.append('| ' + ' | '.join(str(cell) for cell in row) + ' |')
    return '\n'.join(lines)


def render(summary):
    """The generated RESULTS.md section (markers are written by write_section)."""
    summary = summary or {}
    pins = summary.get('pins') or {}
    out = ['## Eval results', '']
    if summary.get('run_id'):
        out.append('run: `%s`' % summary['run_id'])
        out.append('')
    if summary.get('banner'):
        out.extend(['> **%s**' % summary['banner'], ''])
    out.extend(['### Pins', '',
                '- model: %s' % _fmt(pins.get('model')),
                '- provider: %s' % _fmt(pins.get('provider')),
                '- judge model: %s (%s)' % (_fmt(pins.get('judge_model')),
                                            _fmt(pins.get('judge_provider'))),
                '- trials: %s' % _fmt(pins.get('trials')),
                '- hermes version: %s' % _fmt(pins.get('hermes_version')),
                '- repo head: %s' % _fmt(pins.get('repo_head')),
                '- oracle (scripts/checkpoint.py): %s' % _fmt(pins.get('oracle_sha')),
                '- skill fingerprints:'])
    fingerprints = pins.get('fingerprints') or {}
    if fingerprints:
        for cond in sorted(fingerprints):
            out.append('  - %s: %s' % (cond, _fmt(fingerprints[cond])))
    else:
        out.append('  - n/a')
    out.append('')

    out.extend(['### By condition', ''])
    condition_rows = []
    for cond in sorted(summary.get('conditions') or {}):
        stats = summary['conditions'][cond]
        condition_rows.append([cond, _fmt(stats.get('n')),
                               _fmt(stats.get('outcome_mean')),
                               _fmt(stats.get('protocol_mean')),
                               _fmt(stats.get('macro_mean')),
                               _fmt(stats.get('blocker_rate')),
                               _fmt(stats.get('ground_truth_pass_rate')),
                               _fmt(stats.get('gate_ok_rate')),
                               _fmt(stats.get('median_tokens'), 0),
                               _fmt(stats.get('median_wall_s')),
                               _fmt(stats.get('position_unstable')),
                               _fmt(stats.get('final_missing')),
                               _fmt(stats.get('gate_fail_claims')),
                               _fmt(stats.get('completion_claims')),
                               _fmt(stats.get('gate_fail_rate')),
                               _fmt(stats.get('skill_confirmed')),
                               _fmt(stats.get('skill_unconfirmed'))])
    out.append(_table(['condition', 'n', 'outcome', 'protocol', 'composite',
                       'blocker rate', 'ground truth', 'gate ok', 'median tokens',
                       'median wall s', 'unstable', 'final missing',
                       'gate-fail claims', 'claims', 'gate-fail rate',
                       'skill confirmed', 'skill unconfirmed'],
                      condition_rows))
    out.append('')
    out.append('`outcome` is the primary comparison (delivery checks only); `protocol` '
               'is bookkeeping adherence; `composite` is the legacy weighted mix of '
               'outcome, protocol, judged dimensions and cost, kept for continuity only.')
    out.append('')
    evidence = summary.get('skill_evidence') or {}
    if evidence:
        out.append('Skill load evidence: %d candidate run(s); runtime load confirmed in '
                   '%d, unconfirmed in %d (%d with the `-s longhaul` argv preload as the '
                   'only evidence); %d confirmed load(s) matched the pinned fingerprint. '
                   'Unconfirmed runs are scored and listed, never silently dropped. '
                   'Primary comparison available: %s.'
                   % (evidence.get('candidate_runs', 0), evidence.get('confirmed', 0),
                      evidence.get('unconfirmed', 0), evidence.get('preload_argv_only', 0),
                      evidence.get('fingerprint_matched', 0),
                      'yes' if evidence.get('primary_comparison_available') else 'no'))
        out.append('')

    out.extend(['### By case', ''])
    case_rows = []
    for cond in sorted(summary.get('cases') or {}):
        for case in sorted(summary['cases'][cond]):
            stat = summary['cases'][cond][case]
            case_rows.append([case, cond, _fmt(stat.get('outcome')),
                              _fmt(stat.get('protocol')), _fmt(stat.get('mean')),
                              _fmt(stat.get('raw')), _fmt(stat.get('n')),
                              _fmt(stat.get('errors')),
                              _fmt(stat.get('final_missing')),
                              ', '.join(stat.get('blockers') or []) or '-'])
    out.append(_table(['case', 'condition', 'outcome', 'protocol', 'composite', 'raw',
                       'n', 'errors', 'final missing', 'blockers'], case_rows))
    out.append('')

    if summary.get('primary_withheld'):
        evidence = summary.get('skill_evidence') or {}
        out.extend(['### Primary comparison (withheld)'])
        out.append('')
        out.append('The primary comparison is not available because no candidate run '
                   'confirmed the pinned skill at runtime (%d candidate run(s): %d '
                   'confirmed, %d unconfirmed, %d preload-argv only, %d confirmed but '
                   'fingerprint-mismatched). The per-condition and per-case tables '
                   'above are descriptive, not a claim.'
                   % (evidence.get('candidate_runs', 0),
                      evidence.get('confirmed', 0),
                      evidence.get('unconfirmed', 0),
                      evidence.get('preload_argv_only', 0),
                      evidence.get('fingerprint_mismatched', 0)))
        out.append('')
    else:
        out.extend(['### Deltas (primary: outcome-only)'])
        out.append('')
        delta_rows = []
        for case in sorted(summary.get('deltas') or {}):
            pair = summary['deltas'][case]
            delta_rows.append([case,
                               _fmt(pair.get('%s-baseline' % CANDIDATE)),
                               _fmt(pair.get('%s-baseline:protocol_adherence' % CANDIDATE)),
                               _fmt(pair.get('%s-baseline:composite' % CANDIDATE)),
                               _fmt(pair.get('%s-v1' % CANDIDATE))])
        out.append(_table(['case', '%s-baseline outcome' % CANDIDATE,
                           '%s-baseline protocol' % CANDIDATE,
                           '%s-baseline composite' % CANDIDATE,
                           '%s-v1 outcome' % CANDIDATE], delta_rows))
        out.append('')

    out.extend(['### Got worse', ''])
    if summary.get('primary_withheld'):
        out.append('- withheld with the primary comparison')
    elif summary.get('got_worse'):
        for item in summary['got_worse']:
            subject = item.get('case') or item.get('condition') or '?'
            out.append('- %s / %s vs %s: %s (from %s to %s)' % (
                subject, item.get('metric'), item.get('against'),
                _fmt(item.get('delta')), _fmt(item.get('reference')),
                _fmt(item.get('candidate'))))
    else:
        out.append('- none')
    out.append('')

    out.extend(['### Errors', ''])
    if summary.get('errors'):
        for item in summary['errors']:
            out.append('- %s (%s): %s' % (item.get('key'), item.get('case'),
                                          item.get('status')))
    else:
        out.append('- none')
    out.append('')

    out.extend(['### Position unstable', ''])
    if summary.get('unstable'):
        for item in summary['unstable']:
            out.append('- %s t%s %s: %s' % (item.get('case'), item.get('trial'),
                                            item.get('cond'), item.get('dim')))
    else:
        out.append('- none')
    out.append('')

    out.extend(['### Judge unavailable', ''])
    if summary.get('unavailable'):
        for item in summary['unavailable']:
            out.append('- %s t%s: %s' % (item.get('case'), item.get('trial'),
                                         item.get('status')))
    else:
        out.append('- none')
    out.append('')

    out.extend(['### Contamination suspects', ''])
    if summary.get('contamination'):
        for item in summary['contamination']:
            out.append('- %s (%s)' % (item.get('key'), item.get('case')))
    else:
        out.append('- none')
    out.append('')

    if summary.get('unmetered'):
        out.extend(['Unmetered runs (cost null): ' + ', '.join(summary['unmetered']), ''])

    out.extend(['### Raw runs', ''])
    raw_rows = []
    for run in summary.get('runs') or []:
        raw_rows.append([run.get('key'), run.get('cond'), run.get('case'),
                         run.get('trial'), run.get('status'), _fmt(run.get('valid')),
                         _fmt(run.get('outcome')), _fmt(run.get('protocol')),
                         _fmt(run.get('score')), _fmt(run.get('raw')),
                         ', '.join(run.get('blockers') or []) or '-'])
    out.append(_table(['key', 'cond', 'case', 'trial', 'status', 'valid', 'outcome',
                       'protocol', 'composite', 'raw', 'blockers'], raw_rows))
    out.append('')
    return '\n'.join(out).strip('\n')


def write_section(results_md, section):
    """Rewrite only the bytes between the generated markers."""
    path = Path(results_md)
    text = path.read_text(encoding='utf-8')
    if text.count(BEGIN_MARKER) != 1 or text.count(END_MARKER) != 1:
        raise ValueError('RESULTS.md needs exactly one generated:begin and one generated:end')
    start = text.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = text.index(END_MARKER)
    if end < start:
        raise ValueError('generated:end appears before generated:begin')
    body = section.strip('\n')
    new_text = text[:start] + '\n' + body + '\n' + text[end:]
    path.write_text(new_text, encoding='utf-8')
    return new_text


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) < 3:
        print('usage: report.py <measure.jsonl> <judge.jsonl> <header.json> [RESULTS.md]')
        return 2
    measure = [json.loads(line) for line in Path(argv[0]).read_text().splitlines() if line.strip()]
    judge_rows = [json.loads(line) for line in Path(argv[1]).read_text().splitlines()
                  if line.strip()] if Path(argv[1]).is_file() else []
    header = json.loads(Path(argv[2]).read_text())
    summary = aggregate(measure, judge_rows, header)
    body = render(summary)
    if len(argv) > 3:
        write_section(argv[3], body)
    else:
        print(body)
    return 0


if __name__ == '__main__':
    sys.exit(main())
