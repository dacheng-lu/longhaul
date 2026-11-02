<p align="center">
  <img src="assets/logo-readme.svg" alt="Longhaul · 远航 logo" width="640">
</p>

# 远航 · Longhaul

**Code the Journey. Automate the Distance.**  
**以码为桨，以智乘风。**  
**Den Kurs codieren. Die Distanz automatisieren.**

**Sustained progress. Recoverable work. Evidence before completion.**  
**持续推进 · 中断可续 · 凭证交付**  
**Kontinuierlich arbeiten · Aufgaben wiederaufnehmen · Ergebnisse belegen**

[English](#english) · [简体中文](#zh-cn) · [Deutsch](#deutsch)

[Joint recovery demo / 联合恢复演示 / Gemeinsames Wiederaufnahme-Beispiel](docs/makeextreme-handoff.md) · [Goal / 项目目标 / Ziel](GOAL.md) · [First checkpoint example / 检查点入门 / Checkpoint-Beispiel](docs/first-checkpoint.md)

Skill version: **1.2** · [Core protocol / 核心流程 / Kernprotokoll](SKILL.md) · [Apache-2.0](LICENSE)

[Roadmap / 升级路线图 / Entwicklungsplan](ROADMAP.md) — priorities, acceptance criteria and Harness Engineering positioning; proposed work, not shipped features.

## English

**Choosing a tool:** Use Longhaul for exploratory work, decisions and evidence across sessions. Use [Makeextreme](https://github.com/dacheng-lu/makeextreme) for known-stage pipelines with runnable checks. Each works independently; a shared adapter is not implemented. [Try the checkpoint example](docs/first-checkpoint.md).

Longhaul is a lightweight orchestration skill for long-running research, creation and implementation tasks. It helps an AI assistant preserve task state, select available tools, challenge consequential assumptions and verify deliverables before declaring completion.

It combines **durable checkpoints + MCP/tool discovery + selective model/subagent delegation + ABCD decisions + SECI-inspired learning**. Specialist skills remain responsible for domain-specific work and quality checks.

### When to use it

- A task spans multiple stages, files, sources or sessions.
- Work needs to resume after interruption or context loss.
- Repeated failures or conflicting evidence require a change of approach.
- A complex deliverable needs explicit acceptance criteria and a verified handoff.

Routine one-step requests usually do not need Longhaul.

### What v1 includes

| Component | Behavior |
| --- | --- |
| Durable task state | Record the goal, constraints, progress, artifacts, evidence, blockers and next action; reconcile records with actual outputs on recovery. |
| MCP and tool discovery | Inspect callable capabilities first; research official documentation and upstream GitHub sources when a material gap remains. Distinguish discovered tools from connected, authorized tools. |
| Selective delegation | Use supported subagents and models for separable work. Default to no workers for small tasks and at most two concurrent workers for complex tasks, subject to host limits. |
| ABCD + recommendation | Offer distinct options and a reasoned recommendation for meaningful user-owned tradeoffs. Use fewer options when appropriate; investigate factual questions directly. |
| Bounded recovery | Limit unchanged transient attempts and repeated approaches; check whether an external action already succeeded before retrying it. |
| Evidence before completion | Tie evidence to the current artifact revision, run relevant checks and report unresolved gaps honestly. |
| SECI-inspired learning | Turn observations into scoped candidate rules, then test them on another case. Optional rune labels help diagnose stalls. |

### Get started

1. Clone or download this repository, keeping the directory structure intact.
2. Use your assistant host’s supported skill installation flow to register the folder containing `SKILL.md`. Installation paths and supported features vary by host; cloning alone does not activate the skill.
3. Invoke Longhaul explicitly with a concrete goal and deliverable. Persistent storage, tools and subagents must be provided by the host.

```text
Use Longhaul to compare three approaches and deliver a sourced recommendation.
Define acceptance criteria, save recovery checkpoints, and use ABCD with a
recommendation only when my choice materially changes the approach.
```

To resume:

```text
Use Longhaul to resume this task from <checkpoint location>.
Check the actual artifacts and evidence before continuing with the next action.
```

### Checkpoint helper

The helper uses the Python 3 standard library and makes no network requests. **It is Linux-only in v1.2:** every path is walked through directory descriptors with `O_NOFOLLOW` (descriptor-anchored traversal), and a platform that cannot provide that is refused instead of falling back to `realpath()`, so macOS and Windows are not supported. From the repository root:

```bash
python3 scripts/checkpoint.py init ../longhaul-state/task.json --task-id demo-001 --goal "Produce a verified research brief"
python3 scripts/checkpoint.py artifact ../longhaul-state/task.json --id A1 --locator ../brief.md
python3 scripts/checkpoint.py evidence ../longhaul-state/task.json --id E1 --artifact A1 --result pass
# Add acceptance criteria that cite E1, then run the gate.
python3 scripts/checkpoint.py gate ../longhaul-state/task.json
```

`revision` is the first 12 hex of the file's sha256, so nobody bumps a label by hand: register the file again and every piece of evidence bound to the old revision is reported stale, which re-verifies only the criteria that depend on it. `init` refuses to overwrite. A fresh template passes `validate` and **fails `gate`** until criteria are verified. `gate` also rejects a credential-shaped value in the state, an unchanged retry, a third attempt without a hypothesis change, a bundled next action, a future timestamp, an artifact no evidence binds, and a shortcut without an upgrade trigger. It checks bookkeeping and local hashes — **not** semantic correctness or remote results. `migrate` upgrades a v1 record and re-binds evidence only while the content hash still matches. See the [checkpoint contract](references/state.md).

Keep task state outside the installed skill directory and persist it through the host’s durable storage workflow. A temporary local path alone is not a recovery guarantee.

### Boundaries

- Longhaul is an execution protocol, not a background service, scheduler or automatic process-restart system.
- The current repository provides instructions and a checkpoint validator; it does not include a runtime, automatic lifecycle hooks or a model router implementation.
- Tool access, installation, permissions, persistence and model selection depend on the host. Unsupported delegation falls back to sequential work.
- Passing the checkpoint gate is not proof that the work is correct. Domain-specific validation is still required.
- SECI is an engineering analogy here: storing a lesson does not update model weights or demonstrate learning. Rune labels are optional diagnostic aids, not evidence.
- The eval harness compares conditions on fixed fixture scenarios; a higher score is comparative evidence about the skill, not a guarantee that your task's result is correct.
- A live model evaluation is **blocked**: no credential boundary can be proven against an untrusted subject, so no runner API reads, copies or accepts a key and the harness runs the verified offline test double only. A future live pilot needs an authorized, independently audited credential broker.

### Contributing

Useful contributions include reproducible recovery scenarios, clearer host setup instructions and checks that catch unsupported completion claims. Include the expected behavior, observed behavior and verification steps. Never include credentials or private task data.

[Files and design references](#files-and-design-references) · [Back to top](#远航--longhaul)

<a name="zh-cn"></a>

## 简体中文

**如何选择：** 探索性任务、跨会话决策与证据管理使用 Longhaul；阶段已知、可用命令检查的流水线可使用 [Makeextreme](https://github.com/dacheng-lu/makeextreme)。两者可以独立使用；共享适配器尚未实现。[体验检查点示例](docs/first-checkpoint.md)。

远航是面向长期研究、创作与实现任务的轻量编排 Skill，帮助 AI 保存任务状态、选择可用工具、审查关键假设，并在宣称完成之前验证交付物。

它结合了 **持久化检查点 + MCP／工具发现 + 按需模型／子代理委派 + ABCD 决策 + SECI 式经验积累**。具体领域的执行与质量检查仍由相应专业 Skill 负责。

### 适用场景

- 任务跨越多个阶段、文件、来源或会话。
- 中断或上下文丢失后，需要准确续做。
- 重复失败或证据矛盾，需要调整方法。
- 复杂交付物需要明确验收条件和可核验的交接。

普通的一步式请求通常不需要启用远航。

### 第一版包含什么

| 组件 | 行为 |
| --- | --- |
| 持久化任务状态 | 记录目标、约束、进展、产物、证据、阻塞和下一步；恢复时核对真实产物。 |
| MCP 与工具发现 | 先检查当前可调用能力；存在实质缺口时，再研究官方文档与上游 GitHub 源码。明确区分“发现了”与“已接入、已授权”。 |
| 按需委派 | 只使用宿主支持的子代理与模型处理可分离任务。小任务默认不委派，复杂任务默认最多两个并发工作者，并遵守宿主限制。 |
| ABCD＋推荐项 | 对需要用户决定的重要取舍，给出不同路线与推荐理由；适当减少选项，事实问题优先自行查证。 |
| 有限重试与恢复 | 限制无变化的瞬时错误重试和重复路线；重试外部操作前，先检查是否已经成功。 |
| 凭证交付 | 将证据绑定到当前产物版本，执行相应检查，如实说明未解决的问题。 |
| SECI 式经验积累 | 将观察提炼为有适用范围的候选规则，再用其他案例检验；可选符文标签用于诊断阻塞。 |

### 开始使用

1. 克隆或下载本仓库，保留完整目录结构。
2. 通过助手宿主支持的 Skill 安装流程，注册包含 `SKILL.md` 的文件夹。不同宿主的安装路径和能力不同；仅克隆仓库不会自动启用 Skill。
3. 明确提出目标、交付物，并调用远航。持久存储、工具和子代理能力需要宿主提供。

```text
用远航比较三种方案，交付有来源支撑的推荐。
明确验收条件，保存恢复点；只有我的选择会实质改变路线时，
才用 ABCD＋推荐项向我提问。
```

恢复任务：

```text
用远航从 <检查点位置> 继续这个任务。
先核对实际产物和证据，再执行下一步。
```

### 检查点脚本

脚本仅使用 Python 3 标准库，不发起网络请求。**v1.2 仅支持 Linux（Linux-only）**：所有路径都通过目录描述符配合 `O_NOFOLLOW` 逐段遍历（descriptor-anchored），无法提供该能力的平台会被直接拒绝，而不会退回 `realpath()`，因此不支持 macOS 与 Windows。在仓库根目录运行：

```bash
python3 scripts/checkpoint.py init ../longhaul-state/task.json --task-id demo-001 --goal "完成经过核验的研究简报"
python3 scripts/checkpoint.py artifact ../longhaul-state/task.json --id A1 --locator ../brief.md
python3 scripts/checkpoint.py evidence ../longhaul-state/task.json --id E1 --artifact A1 --result pass
# 写入引用 E1 的验收条件后，执行最终检查。
python3 scripts/checkpoint.py gate ../longhaul-state/task.json
```

`revision` 是文件 sha256 的前 12 位，因此不需要手工递增版本号：再次登记同一文件就会产生新版本，绑定旧版本的证据被标记为过期，只有依赖它的验收条件需要重新核验。`init` 拒绝覆盖已有文件；新模板可以通过 `validate`，但在验收条件通过前**必然无法通过 `gate`**。`gate` 还会拒绝：状态中的凭证形态数值、原样重试、同一阻塞第三次仍未更换假设、打包多步的下一步动作、未来时间戳、没有任何证据绑定的产物，以及没有升级触发的捷径记录。它只检查记账与本地哈希，**不**判断内容是否正确，也不核验远程结果。`migrate` 把 v1 记录升级为 v2，并且只在内容哈希仍然一致时重新绑定证据。详见[检查点约定](references/state.md)。

将任务状态保存在已安装 Skill 目录之外，并通过宿主的持久存储流程保存。仅有一个临时本地路径，不能保证任务可恢复。

### 能力边界

- 远航是执行协议，不是后台服务、调度器或进程自动重启系统。
- 当前仓库提供规则与检查点验证器，尚未包含运行时、自动生命周期钩子或模型路由实现。
- 工具接入、安装、权限、持久化与模型选择取决于宿主；无法委派时，改为顺序执行。
- 检查点通过不等于内容正确，仍需完成领域内的质量验证。
- SECI 在这里是工程类比：保存经验不代表更新模型权重，也不能单独证明学习有效。符文标签仅用于辅助诊断。
- 评测框架只在固定场景上比较不同条件；得分只是关于 Skill 的相对证据，不代表你的任务结果一定正确。
- 真实模型的在线评测目前**被阻断**：无法证明凭证边界对不受信任的受测方成立，因此任何运行器接口都不读取、不复制、不接受密钥，评测只运行经过校验的离线测试替身。未来的在线试点需要单独授权并独立审计的凭证代理。

### 参与改进

欢迎提供可复现的恢复案例、更清晰的宿主配置说明，以及能够识别无证据完成声明的检查。请附预期行为、实际行为和验证步骤，不要提交凭证或私人任务数据。

[文件与设计参考](#files-and-design-references) · [返回顶部](#远航--longhaul)

## Deutsch

**Werkzeugwahl:** Longhaul unterstützt offene Aufgaben, Entscheidungen und Belege über Sitzungen hinweg; [Makeextreme](https://github.com/dacheng-lu/makeextreme) führt Pipelines mit bekannten Stufen und prüfbaren Ergebnissen aus. Beide funktionieren unabhängig; ein gemeinsamer Adapter ist nicht implementiert. [Checkpoint-Beispiel](docs/first-checkpoint.md).

Longhaul ist ein schlanker Orchestrierungs-Skill für länger dauernde Recherche-, Gestaltungs- und Implementierungsaufgaben. Er hilft einem KI-Assistenten, den Aufgabenstand zu sichern, verfügbare Werkzeuge auszuwählen, wichtige Annahmen zu hinterfragen und Ergebnisse vor dem Abschluss zu prüfen.

Er verbindet **dauerhafte Checkpoints + MCP- und Werkzeugerkennung + gezielten Einsatz von Modellen und Subagenten + ABCD-Entscheidungen + SECI-inspirierte Wissensentwicklung**. Fachspezifische Skills übernehmen weiterhin die eigentliche Facharbeit und deren Qualitätsprüfung.

### Wann ist Longhaul sinnvoll?

- Eine Aufgabe umfasst mehrere Phasen, Dateien, Quellen oder Sitzungen.
- Nach einer Unterbrechung oder einem Kontextverlust soll die Arbeit fortgesetzt werden.
- Wiederholte Fehler oder widersprüchliche Belege erfordern einen anderen Ansatz.
- Ein komplexes Ergebnis benötigt klare Abnahmekriterien und eine nachvollziehbare Übergabe.

Einfache Aufgaben mit nur einem Schritt benötigen Longhaul normalerweise nicht.

### Funktionsumfang von Version 1

| Baustein | Verhalten |
| --- | --- |
| Dauerhafter Aufgabenstand | Ziel, Vorgaben, Fortschritt, Artefakte, Belege, Hindernisse und nächsten Schritt festhalten; beim Fortsetzen mit den tatsächlichen Ergebnissen abgleichen. |
| MCP- und Werkzeugerkennung | Zuerst verfügbare Funktionen prüfen; bei wesentlichen Lücken offizielle Dokumentation und ursprüngliche GitHub-Quellen untersuchen. Gefundene Werkzeuge von verbundenen und autorisierten Werkzeugen unterscheiden. |
| Gezielte Delegation | Unterstützte Subagenten und Modelle für klar abgrenzbare Teilaufgaben einsetzen. Standardmäßig keine Delegation bei kleinen Aufgaben und höchstens zwei parallele Subagenten bei komplexen Aufgaben, innerhalb der Grenzen der Host-Umgebung. |
| ABCD mit Empfehlung | Für wichtige Nutzerentscheidungen unterschiedliche Optionen mit begründeter Empfehlung anbieten; bei Bedarf weniger Optionen verwenden. Sachfragen selbst recherchieren. |
| Begrenzte Wiederholungen | Unveränderte Wiederholungsversuche und erfolglose Ansätze begrenzen; vor der Wiederholung externer Aktionen prüfen, ob sie bereits erfolgreich waren. |
| Belege vor dem Abschluss | Nachweise der aktuellen Artefaktversion zuordnen, relevante Prüfungen durchführen und offene Punkte ehrlich benennen. |
| SECI-inspirierte Wissensentwicklung | Beobachtungen in vorläufige Regeln mit klarem Geltungsbereich überführen und an anderen Fällen prüfen. Optionale Runenbezeichnungen unterstützen die Diagnose von Blockaden. |

### Einstieg

1. Dieses Repository klonen oder herunterladen und die Verzeichnisstruktur beibehalten.
2. Den Ordner mit `SKILL.md` über das unterstützte Installationsverfahren der verwendeten KI-Umgebung als Skill registrieren. Pfade und Funktionen unterscheiden sich je nach Umgebung; das Klonen allein aktiviert den Skill nicht.
3. Longhaul ausdrücklich mit einem konkreten Ziel und gewünschten Ergebnis aufrufen. Dauerhafte Speicherung, Werkzeuge und Subagenten müssen von der Host-Umgebung bereitgestellt werden.

```text
Nutze Longhaul, um drei Ansätze zu vergleichen und eine quellenbasierte
Empfehlung zu erstellen. Definiere Abnahmekriterien und sichere Checkpoints.
Frage mich nur dann mit ABCD und einer Empfehlung, wenn meine Entscheidung
den weiteren Ansatz wesentlich verändert.
```

Aufgabe fortsetzen:

```text
Setze diese Aufgabe mit Longhaul anhand von <Checkpoint-Speicherort> fort.
Prüfe die tatsächlichen Artefakte und Belege, bevor du weiterarbeitest.
```

### Checkpoint-Hilfsskript

Das Skript verwendet ausschließlich die Python-3-Standardbibliothek und sendet keine Netzwerkanfragen. **Unterstützt wird in v1.2 nur Linux (Linux-only):** jeder Pfad wird über Verzeichnis-Deskriptoren mit `O_NOFOLLOW` komponentenweise durchlaufen (descriptor-anchored); eine Plattform, die das nicht bereitstellt, wird abgelehnt statt auf `realpath()` zurückzufallen. macOS und Windows werden daher nicht unterstützt. Im Stammverzeichnis des Repositorys ausführen:

```bash
python3 scripts/checkpoint.py init ../longhaul-state/task.json --task-id demo-001 --goal "Einen geprüften Recherchebericht erstellen"
python3 scripts/checkpoint.py artifact ../longhaul-state/task.json --id A1 --locator ../brief.md
python3 scripts/checkpoint.py evidence ../longhaul-state/task.json --id E1 --artifact A1 --result pass
# Abnahmekriterien eintragen, die E1 referenzieren, dann die Abschlussprüfung starten.
python3 scripts/checkpoint.py gate ../longhaul-state/task.json
```

`revision` sind die ersten 12 Hex-Zeichen des sha256 der Datei — niemand zählt eine Version von Hand hoch: wird die Datei erneut registriert, gilt jeder an die alte Revision gebundene Beleg als veraltet, und nur die davon abhängigen Kriterien müssen erneut geprüft werden. `init` überschreibt nichts; eine neue Vorlage besteht `validate` und **scheitert an `gate`**, solange Kriterien nicht verifiziert sind. `gate` weist außerdem zurück: einen zugangsdatenförmigen Wert im Zustand, einen unveränderten Wiederholungsversuch, einen dritten Versuch ohne Hypothesenwechsel, einen gebündelten nächsten Schritt, einen Zeitstempel in der Zukunft, ein Artefakt ohne Beleg und eine Abkürzung ohne Auslöser für die Rückkehr. Geprüft werden Buchhaltung und lokale Hashes — **nicht** inhaltliche Richtigkeit oder entfernte Ergebnisse. `migrate` hebt einen v1-Datensatz auf v2 und bindet Belege nur neu, solange der Inhalts-Hash noch passt. Details: [Checkpoint-Spezifikation](references/state.md).

Den Aufgabenstand außerhalb des installierten Skill-Verzeichnisses ablegen und über die dauerhafte Speicherfunktion der Host-Umgebung sichern. Ein temporärer lokaler Pfad allein garantiert keine Wiederaufnahme.

### Grenzen

- Longhaul ist ein Ausführungsprotokoll, kein Hintergrunddienst, Zeitplaner oder System für automatische Prozessneustarts.
- Das Repository enthält Anweisungen und einen Checkpoint-Validator, aber noch keine Laufzeitumgebung, automatischen Lifecycle-Hooks oder implementierte Modellweiterleitung.
- Werkzeugzugriff, Installation, Berechtigungen, Speicherung und Modellauswahl hängen von der Host-Umgebung ab. Ohne Delegationsfunktion wird sequenziell gearbeitet.
- Eine bestandene Checkpoint-Prüfung beweist nicht die Richtigkeit der Ergebnisse. Fachspezifische Qualitätsprüfungen bleiben erforderlich.
- SECI dient hier als technische Analogie: Das Speichern einer Erkenntnis verändert keine Modellgewichte und belegt noch keinen Lernerfolg. Runenbezeichnungen sind optionale Diagnosehilfen.
- Die Eval-Harness vergleicht Bedingungen an festen Szenarien; ein höherer Score ist ein vergleichender Beleg über den Skill, keine Garantie für die Richtigkeit der eigenen Aufgabe.
- Eine Live-Evaluierung mit echtem Modell ist **blockiert**: Eine Credential-Grenze lässt sich gegenüber einem nicht vertrauenswürdigen Subjekt nicht beweisen, daher liest, kopiert und akzeptiert keine Runner-API einen Schlüssel; die Harness führt nur den geprüften Offline-Testdoppelgänger aus. Ein späterer Live-Pilot braucht einen autorisierten, unabhängig geprüften Credential-Broker.

### Mitwirken

Willkommen sind reproduzierbare Wiederaufnahmeszenarien, verständlichere Einrichtungsanleitungen und Prüfungen gegen unbelegte Abschlussmeldungen. Bitte erwartetes und beobachtetes Verhalten sowie Prüfschritte angeben. Keine Zugangsdaten oder privaten Aufgabendaten einreichen.

[Dateien und Entwurfsquellen](#files-and-design-references) · [Zurück zum Anfang](#远航--longhaul)

## Files and design references

The implementation documents are currently in English. 本节链接的实现文档目前使用英语。 Die verlinkten Implementierungsdokumente sind derzeit auf Englisch.

| File / 文件 / Datei | Purpose / 用途 / Zweck |
| --- | --- |
| [SKILL.md](SKILL.md) | Core protocol · 核心流程 · Kernprotokoll |
| [references/state.md](references/state.md) | Checkpoint contract · 检查点约定 · Checkpoint-Spezifikation |
| [references/capabilities.md](references/capabilities.md) | Tools and delegation · 工具与委派 · Werkzeuge und Delegation |
| [references/grill.md](references/grill.md) | ABCD decision review · ABCD 决策审查 · ABCD-Entscheidungsprüfung |
| [references/learning.md](references/learning.md) | SECI and lessons · SECI 与经验积累 · SECI und Lektionen |
| [scripts/checkpoint.py](scripts/checkpoint.py) | State validator · 状态验证器 · Zustandsvalidator |
| [scripts/secret_scan.py](scripts/secret_scan.py) | Credential detector · 凭证检测器 · Zugangsdaten-Erkennung |
| [tests/test_checkpoint.py](tests/test_checkpoint.py) | Gate tests · 校验测试 · Gate-Tests |
| [agents/openai.yaml](agents/openai.yaml) | Host interface metadata · 宿主界面元数据 · Metadaten für die Host-Oberfläche |
| [assets/icon.svg](assets/icon.svg) | Skill icon · Skill 图标 · Skill-Symbol |

Design inspirations, not bundled dependencies or claims of affiliation:  
以下项目为设计参考，不是内置依赖，也不代表官方关联：  
Entwurfsquellen, keine mitgelieferten Abhängigkeiten oder behaupteten Partnerschaften:

- [Planning with Files](https://github.com/OthmanAdi/planning-with-files): file-backed task state / 文件化任务状态 / dateibasierter Aufgabenstand.
- [Superpowers](https://github.com/obra/superpowers): evidence before completion / 完成前验证 / Belege vor dem Abschluss.
- [LangGraph](https://github.com/langchain-ai/langgraph): runtime persistence patterns / 运行时持久化模式 / Persistenzmuster für Laufzeitumgebungen.
- [Reflexion](https://github.com/noahshinn/reflexion): feedback across attempts / 跨尝试反馈 / Feedback zwischen Versuchen.
- [Nonaka, Toyama & Konno (2000)](https://doi.org/10.1016/S0024-6301(99)00115-6): SECI theory; its application here is a design proposal / SECI 理论，此处的 AI 应用属于设计提案 / SECI-Theorie; die Anwendung auf KI ist hier ein Entwurfsvorschlag.

License / 许可证 / Lizenz: [Apache License 2.0](LICENSE).
