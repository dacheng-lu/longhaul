# 远航 · Longhaul

> **Code the Journey. Automate the Distance.**  
> **以码为桨，以智乘风**  
> **Den Kurs codieren. Die Distanz automatisieren.**

**Sustained progress. Recoverable work. Evidence before completion.**  
**持续推进 · 中断可续 · 凭证交付**  
**Kontinuierlich arbeiten · Aufgaben wiederaufnehmen · Ergebnisse belegen**

[English](#english) · [简体中文](#zh-cn) · [Deutsch](#deutsch)

Skill version: **1.0** · [Core protocol / 核心流程 / Kernprotokoll](SKILL.md) · [Apache-2.0](LICENSE)

## English

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

The helper uses the Python 3 standard library and makes no network requests. From the repository root:

```bash
python3 scripts/checkpoint.py init ../longhaul-state/task.json --task-id demo-001 --goal "Produce a verified research brief"
python3 scripts/checkpoint.py validate ../longhaul-state/task.json
# Fill in acceptance criteria, deliverables and evidence before the final gate.
python3 scripts/checkpoint.py gate ../longhaul-state/task.json
```

`init` refuses to overwrite an existing file. A fresh template can pass `validate`, but **must fail `gate`** until the required records are filled. `gate` checks acceptance status, evidence revisions, unresolved blockers and local file hashes. It does **not** verify semantic correctness or remote results. See the [checkpoint contract](references/state.md).

Keep task state outside the installed skill directory and persist it through the host’s durable storage workflow. A temporary local path alone is not a recovery guarantee.

### Boundaries

- Longhaul is an execution protocol, not a background service, scheduler or automatic process-restart system.
- The current repository provides instructions and a checkpoint validator; it does not include a runtime, automatic lifecycle hooks or a model router implementation.
- Tool access, installation, permissions, persistence and model selection depend on the host. Unsupported delegation falls back to sequential work.
- Passing the checkpoint gate is not proof that the work is correct. Domain-specific validation is still required.
- SECI is an engineering analogy here: storing a lesson does not update model weights or demonstrate learning. Rune labels are optional diagnostic aids, not evidence.

### Contributing

Useful contributions include reproducible recovery scenarios, clearer host setup instructions and checks that catch unsupported completion claims. Include the expected behavior, observed behavior and verification steps. Never include credentials or private task data.

[Files and design references](#files-and-design-references) · [Back to top](#远航--longhaul)

<a name="zh-cn"></a>

## 简体中文

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

脚本仅使用 Python 3 标准库，不发起网络请求。在仓库根目录运行：

```bash
python3 scripts/checkpoint.py init ../longhaul-state/task.json --task-id demo-001 --goal "完成经过核验的研究简报"
python3 scripts/checkpoint.py validate ../longhaul-state/task.json
# 填写验收条件、交付物和证据后，再执行最终检查。
python3 scripts/checkpoint.py gate ../longhaul-state/task.json
```

`init` 拒绝覆盖已有文件。新模板可以通过 `validate`，但在必要记录填写完整前，**应当无法通过 `gate`**。`gate` 检查验收状态、证据版本、未解决的阻塞和本地文件哈希，**不会**判断内容是否正确，也不会核验远程结果。详见[检查点约定](references/state.md)。

将任务状态保存在已安装 Skill 目录之外，并通过宿主的持久存储流程保存。仅有一个临时本地路径，不能保证任务可恢复。

### 能力边界

- 远航是执行协议，不是后台服务、调度器或进程自动重启系统。
- 当前仓库提供规则与检查点验证器，尚未包含运行时、自动生命周期钩子或模型路由实现。
- 工具接入、安装、权限、持久化与模型选择取决于宿主；无法委派时，改为顺序执行。
- 检查点通过不等于内容正确，仍需完成领域内的质量验证。
- SECI 在这里是工程类比：保存经验不代表更新模型权重，也不能单独证明学习有效。符文标签仅用于辅助诊断。

### 参与改进

欢迎提供可复现的恢复案例、更清晰的宿主配置说明，以及能够识别无证据完成声明的检查。请附预期行为、实际行为和验证步骤，不要提交凭证或私人任务数据。

[文件与设计参考](#files-and-design-references) · [返回顶部](#远航--longhaul)

## Deutsch

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

Das Skript verwendet ausschließlich die Python-3-Standardbibliothek und sendet keine Netzwerkanfragen. Im Stammverzeichnis des Repositorys ausführen:

```bash
python3 scripts/checkpoint.py init ../longhaul-state/task.json --task-id demo-001 --goal "Einen geprüften Recherchebericht erstellen"
python3 scripts/checkpoint.py validate ../longhaul-state/task.json
# Vor der Abschlussprüfung Abnahmekriterien, Ergebnisse und Belege eintragen.
python3 scripts/checkpoint.py gate ../longhaul-state/task.json
```

`init` überschreibt keine vorhandene Datei. Eine neue Vorlage kann `validate` bestehen, **muss aber bei `gate` scheitern**, solange die erforderlichen Einträge fehlen. `gate` prüft den Abnahmestatus, die Versionen der Belege, offene Hindernisse und lokale Datei-Hashes. Inhaltliche Richtigkeit und entfernte Ergebnisse werden **nicht** geprüft. Details stehen in der [Checkpoint-Spezifikation](references/state.md).

Den Aufgabenstand außerhalb des installierten Skill-Verzeichnisses ablegen und über die dauerhafte Speicherfunktion der Host-Umgebung sichern. Ein temporärer lokaler Pfad allein garantiert keine Wiederaufnahme.

### Grenzen

- Longhaul ist ein Ausführungsprotokoll, kein Hintergrunddienst, Zeitplaner oder System für automatische Prozessneustarts.
- Das Repository enthält Anweisungen und einen Checkpoint-Validator, aber noch keine Laufzeitumgebung, automatischen Lifecycle-Hooks oder implementierte Modellweiterleitung.
- Werkzeugzugriff, Installation, Berechtigungen, Speicherung und Modellauswahl hängen von der Host-Umgebung ab. Ohne Delegationsfunktion wird sequenziell gearbeitet.
- Eine bestandene Checkpoint-Prüfung beweist nicht die Richtigkeit der Ergebnisse. Fachspezifische Qualitätsprüfungen bleiben erforderlich.
- SECI dient hier als technische Analogie: Das Speichern einer Erkenntnis verändert keine Modellgewichte und belegt noch keinen Lernerfolg. Runenbezeichnungen sind optionale Diagnosehilfen.

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
| [references/learning.md](references/learning.md) | SECI and diagnostics · SECI 与诊断 · SECI und Diagnose |
| [scripts/checkpoint.py](scripts/checkpoint.py) | State validator · 状态验证器 · Zustandsvalidator |
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
