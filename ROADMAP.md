# Longhaul roadmap / 升级路线图

Status: proposed roadmap, not a release commitment or a claim of implemented capability.
状态：待实施建议，不代表功能已经实现，也不承诺发布日期。

Reviewed: 2026-11-01, against main at a2e10c571362c4b5224c3d8ed36393796f45c62a.
Version labels below are planning suggestions. Re-check current code and results before implementation.

## Positioning / 项目定位

**Longhaul is a recovery and verification layer for long-running agents, in the field of Harness Engineering.**

**远航是面向长任务 Agent 的恢复与验证层，属于 Harness Engineering 方向。**

Its current form combines a skill protocol, a checkpoint validator and an evaluation harness. The host supplies model/tool execution, permissions and durable storage. Instructions to run a gate are not equivalent to a host-enforced lifecycle gate. The evaluation sandbox does not automatically protect ordinary user tasks.

当前形态是 Skill 协议＋检查点验证工具＋评测框架。模型与工具执行、权限和持久存储由宿主提供。要求 Agent 主动调用 gate，不等于宿主强制执行 gate；评测环境的隔离措施也不会自动成为实际任务的保障。

## Baseline / 当前基线

- v1.0 established durable state, capability discovery, bounded delegation, decisions and learning guidance.
- v1.1 added enforceable checkpoint rules and evidence bound to individual artifacts.
- v1.2 added measurement infrastructure and hardened checkpoint handling; the helper and harness are Linux-only.
- At the reviewed revision, live model evaluation is blocked and the baseline pilot has not run. Deterministic tests establish infrastructure behavior, not improved real-task success.
- The documented comparison conditions are baseline, v1 and v1.1. They do not directly evaluate the complete current v1.2 package.

Sources of truth: [CHANGELOG](CHANGELOG.md), [protocol](SKILL.md), [evaluation status and residual risks](evals/RESULTS.md).

## P0 — v1.2.x: consistent contracts / 统一协议与实现

- [ ] Resolve the apparent conflict between bounded unchanged transient retries and rejection of an unchanged retry. Define transient-error eligibility, attempt budgets and checks for already-completed external effects; cover allowed and rejected examples.
- [ ] Synchronize README descriptions with the current protocol, including obsolete rune references and version-specific headings.
- [ ] Make supported platforms, host responsibilities and live-evaluation restrictions explicit in setup examples.
- [ ] Distinguish instructions, validator-enforced rules and host-enforced behavior in documentation.

Acceptance: prose, examples and tests agree on the same representative scenarios; no documented workflow depends on unavailable capabilities.

验收：相同场景下，协议、示例与测试结论一致；明确哪些保障需要宿主实现。

## Known open findings — candidate v1.2.1 / 已知未修的审计发现

Found by an independent audit of the v1.2 CI fix (the report is not in this repository). The audit rated the first three MINOR and the last one PLAN. Each was reproduced against `main` at `5611858` before being listed. None is fixed yet; this section records them and does not schedule them.

由对 v1.2 CI 修复的独立审计发现（报告不在本仓库）。审计将前三项评为 MINOR，最后一项评为 PLAN。每一项都在 `main`（`5611858`）上重现过才列出。尚未修复；本节只记录，不排期。

- [ ] `tests/test_release_contract.py`: `test_the_checkout_fetches_the_full_history` asserts `'fetch-depth: 0' in step`, a substring match that a commented-out line would also satisfy. Match the line itself.
- [ ] `evals/scripts/casespec.py`: for the two control cases (`control-trivial`, `control-finish`) the validator accepts a dangling symlink at `golden/overlay` without an error (`validate` prints `"ok": true`).
- [ ] `tests/test_eval_cases.py`: nothing shows that a control case still needs its `reply.md` and `events.jsonl` after the empty-overlay exemption. With that requirement removed from `_check_golden`, all 97 tests in the file still pass.
- [ ] `tests/test_eval_runner.py`: the two pin tests skip on a shallow checkout instead of failing. With `CI` set they should fail, so a shallow checkout cannot pass silently.

Acceptance: each item has a test that fails before the fix and passes after it, and the suite stays green on a fresh full-history clone with `TMPDIR`, `TEMP` and `TMP` unset.

验收：每一项都有修复前失败、修复后通过的测试；在清除 `TMPDIR`、`TEMP`、`TMP` 的全新完整克隆里，整套测试仍为绿。

## P0 — v1.3: measure real benefit / 验证真实收益

### Experimental design

- [ ] Separate a revisable development set, a frozen test set and a held-out generalization set.
- [ ] Audit prompts for leakage directly. Do not treat a strong baseline alone as proof of contamination or repeatedly tune the final test set until baseline performance drops.
- [ ] Add the current released candidate as an explicitly pinned condition. Record skill, helper, oracle, model, provider and runner versions.
- [ ] Predeclare trial counts, task sampling, exclusions and primary metrics; report uncertainty and per-case results. Exploratory runs must remain labelled exploratory.
- [ ] Keep outcome quality separate from protocol compliance; include controls against needless hesitation.
- [ ] Publish failures, regressions and null results alongside improvements.
- [ ] Run ablations: checkpoints only; plus evidence binding; plus recovery policy; plus learning guidance. Keep model and task budgets comparable.

### Safe live-evaluation prerequisite

- [ ] Define the threat model and independently review the credential/execution boundary before enabling live evaluation.
- [ ] Review the existing residual-risk inventory, especially subject-written inputs, log-copy symlinks, cleanup ownership and concurrent invocation.
- [ ] Add process ownership and locking sufficient to prevent two invocations corrupting shared evaluation state.
- [ ] Preserve fail-closed behavior until the live prerequisites are satisfied. Secret scanning and source-code tripwires are not substitutes for isolation.

These are future implementation prerequisites, not authorization to enable a live lane or use credentials.

### Metrics

| Metric | Question |
| --- | --- |
| Task success / 任务成功率 | Was the requested work actually completed? |
| Unsupported completion claims / 无依据完成声明率 | Did the agent claim more than its evidence supports? |
| Recovery accuracy / 恢复正确率 | Did it resume the correct task and reconcile changes? |
| Duplicated effort / 重复工作量 | How much valid work was unnecessarily repeated? |
| User questions / 用户提问次数 | How much avoidable interaction did the protocol add? |
| Tokens, time, tool calls / 成本 | What additional resources bought the reliability gain? |

Acceptance: a reproducible real-model report with pinned conditions, uncertainty, costs, exclusions and negative results; no effectiveness claim based only on test doubles.

验收：能够回答“在什么任务中，以多少额外成本，减少了哪些失败”，并允许结论为无提升。

## P1 — v1.4: usable recovery / 降低使用门槛

- [ ] Add an environment diagnostic command for platform support, storage and required capabilities.
- [ ] Provide one minimal end-to-end example: create a task, interrupt it, change an artifact, resume, invalidate stale evidence and reverify.
- [ ] Publish host-specific setup guides only for integrations actually exercised; cloning alone must not be described as installation.
- [ ] Separate protocol-only use from Linux helper/harness support and state the reduced guarantees of each.
- [ ] Demonstrate a durable handoff locator that works in a new session.

Acceptance: a new user completes the interruption/recovery example from documented instructions without maintainer intervention.

验收：新用户能够独立完成一次“中断—恢复—验证”，并理解平台与宿主限制。

## P1 — later: host lifecycle integration / 接入宿主执行生命周期

- [ ] Define a versioned adapter contract for task IDs, artifact revisions, evidence, failures and external-action receipts.
- [ ] Integrate checkpointing at verified milestones and handoff boundaries where host APIs support it.
- [ ] Make completion checks host-enforced where possible; explicitly identify integrations that still rely on voluntary agent compliance.
- [ ] Reconcile external effects before retries; use idempotency keys where supported.
- [ ] Test interrupted writes, repeated callbacks, concurrent writers and host restart.
- [ ] Keep a documented sequential fallback when delegation is unavailable.

Acceptance: a supported adapter detects stale evidence at completion and can resume after interruption without duplicating an already-successful external effect.

验收：把协议要求接入可观察、可测试的宿主行为；无需为了这一目标重新实现完整 Agent 运行时。

## P2 — later: validate reusable lessons / 验证经验是否有效

- [ ] Keep candidate, tested and rejected lessons separate from task progress.
- [ ] Record scope, evidence, counterexamples and the next discriminating test.
- [ ] Test lessons on materially different tasks and retire rules that increase cost without benefit.
- [ ] Require explicit authorization before changing permanent skills; never infer weight updates from stored lessons.

Acceptance: a lesson can be rejected by evidence, and any claimed improvement is supported outside its original example.

## Proposal — v2.0 trust substrate (not decided) / 提议（未决定）

Design only, not scheduled, and deliberately conditional: the v1.3 live-benefit measurement
is not finished, so nothing here is built beyond what that measurement justifies. If v1.3
shows no benefit over the current workflow, this direction is withdrawn.

提议，未决定，不排期。仅设计；v1.3 的真实收益测量尚未完成，因此在该测量证明有必要之前不实现。
若 v1.3 显示没有收益，则撤回此方向。

- [docs/proposals/trust-substrate-2.0.md](docs/proposals/trust-substrate-2.0.md): immutable Mandates, a phase decision table, stateless replayable verdicts, independent verification (a producer cannot certify itself), a gate run by the host on Makeextreme's evidence, and cluster evaluation cases (racing workers, forged completion, stale peer writes, injected instructions, revoked Mandate). The shared exchange format with Makeextreme and FlowLore is drafted as `trio-trust/0` and is not frozen until one real release-digest run passes.
- Acceptance cases are listed in the proposal with negative controls. Attention cost (interruptions per delivered task) is reported, never optimised by hiding a problem.

## Recommended next milestone / 推荐下一步

Complete contract consistency, then freeze a small evaluation set and establish a safe live pilot. Prioritize measured recovery and verification benefits before adding more orchestration features.

先统一协议，再冻结小规模测试集并满足安全在线试点前提。优先证明恢复与验证的实际收益，再扩展编排功能。

## Reading / 术语参考

These sources explain the broader field; they are not endorsements or evidence of Longhaul's effectiveness.

- [OpenAI: Harness engineering](https://openai.com/index/harness-engineering/)
- [Anthropic: Effective harnesses for long-running agents](https://www.anthropic.com/research/effective-harnesses-for-long-running-agents)
