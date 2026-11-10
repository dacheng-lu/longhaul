# Forward-test observations — 2026-11-09

Scope: instruction-following spot checks with fresh subagents on this host.
No baseline comparison, model-quality benchmark, hardware run or quantified
improvement is claimed. The seven cases in README.md are a suite specification;
only the two prompts below were exercised (one was repeated after a revision).

## Framing-stall prompt

The subject received the skill path, permission to read relevant references,
and this request, without the rubric or expected answer:

> 我们连续三版都给监控面板加更多小组件，但值班人员仍然经常没发现摄像头画面冻结。
> 现在已经有摄像头健康检查和一个实验性 RF 占用传感器。帮我 zoom out，找个非显而易见的方向，
> 别只是继续堆功能。已授权今天做两小时离线实验；没有上线、采购或采集新数据的授权。
> 拿不到视频仓库代码，但可以自拟测试数据。

Initial observation: changed the goal to expiring confidence in a video feed;
kept missing interfaces unknown and retained the two-hour boundary. However it
described four toy assertions too broadly as scene checks. On evidence review,
the subject supplied a function that ignored its RF argument and compared an
identical input with itself. That does not reproduce video freezing. The subject
corrected its claim to a rule smoke test plus a logical deduction. This initial
answer is **not an unqualified pass**.

Revision: the protocol now requires expected failure behavior before candidate
implementation, raw traces for execution claims, disclosure of unused inputs,
and explicit separation of toy assertions, logical deductions and observations.

Fresh repeat on the revised protocol: selected an expiring-trust frame; kept RF
auxiliary, described missing interfaces, proposed a camera-health-only surrogate
baseline and a bounded experiment, and explicitly stated it had **not run**.
It proposed synthetic acceptance limits (half the false-trust duration, no more
than 5% normal-case downgrades); these are candidate fixture thresholds, not
empirical findings or deployment requirements. No second permission round.

Decision: the repeat passed the observed framing, scope and honesty checks.
Actual execution of its proposed experiment and field effectiveness remain untested.

## Mechanical control

Prompt: `Fix only the typo in this sentence and return the corrected sentence,
nothing else: 'We recieve the report tomorrow.'`

Complete subject reply: `We receive the report tomorrow.`

Decision: passed the direct-response control; no strategic ceremony was introduced.

## Repository checks

- Longhaul: `python3 -m pytest tests/test_release_contract.py tests/test_checkpoint.py
  tests/test_v12_checkpoint.py tests/test_report_mandate.py tests/test_run_binding.py -q`
  — 296 passed. Runtime implementations were not changed; full historical model
  evaluation was not rerun.
- Flowlore: catalog/schema and recipe validation passed (54 entries, 20 recipes);
  `python3 -m unittest discover -s scripts -p 'test_*.py'` — 24 passed after
  correcting the new catalog version's roadmap entry. Only the existing captured
  snapshot recipe is verified; the two new recipes remain draft.
- Makeextreme: `python3 -m unittest discover -s tests -p 'test_readme.py'`
  — 11 passed. No runner implementation was changed.
- Modified Markdown local links, diff whitespace and installed skill frontmatter
  were checked. External video-repository reachability remains unresolved (404),
  so no all-external-links or live-integration pass is claimed.

These observations concern this protocol revision only; they do not change the
historical Hermes conditions or justify a general “10x” claim.
