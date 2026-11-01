"""R8: the spawn proof is a runtime audit hook; the AST scan is a tripwire.

R7's scanner was a catalogue of function names and missed ``os.system`` in a
handler (IN8). R8 changes the order of evidence:

* :func:`test_no_cli_route_spawns_before_the_gate` is the PRIMARY proof. It runs
  the real CLI in a child interpreter with ``sys.addaudithook`` installed, walks
  every subcommand *and every store_true flag* the parser declares, and requires
  that a non-double ``--hermes`` is refused before any of ``subprocess.Popen``,
  ``subprocess.run``, ``os.system``, ``os.exec*``, ``os.posix_spawn``,
  ``os.fork``, ``os.spawn*`` or ``pty.spawn`` fires. Only the checkpoint oracle
  on repository fixtures may spawn, and then only
  ``[python, scripts/checkpoint.py, gate, …]``.
* :func:`test_every_cli_handler_gates_before_any_other_call` is the STRUCTURAL
  proof (R8c): for every function that owns an ``offline_gate`` call, every call
  lexically before the gate must be in a small reviewed allow-list. A conditional
  spawn (``if args.trials != 1``) or an evasive spelling (``import os as _o``,
  ``__import__('subprocess')``) is simply a call that is not on the list.
* The AST scan stays as a tripwire for *change review*: it discovers calls and
  non-call references in the dangerous namespaces (aliases at any import depth,
  ``getattr``, ``importlib``, ``__import__``, ``mod.subprocess``, keyword
  ``args=``, ``*cmd``, f-strings), and only the one runner-owned spawn plus a
  small, explicitly reviewed maintenance table may be classified. Anything else
  is ``unclassified`` and fails.
"""
import argparse
import ast
import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
EVALS = REPO / 'evals'
HERMES = EVALS / 'runners' / 'hermes.py'
RUN_EVALS = EVALS / 'scripts' / 'run_evals.py'
JUDGE = EVALS / 'scripts' / 'judge.py'

SUBPROCESS_NAMES = frozenset({'run', 'Popen', 'call', 'check_call', 'check_output',
                              'getoutput', 'getstatusoutput'})
OS_NAMES = frozenset({'system', 'popen', 'fork', 'forkpty', 'execv', 'execve', 'execvp',
                      'execvpe', 'execl', 'execle', 'execlp', 'execlpe', 'spawnv',
                      'spawnve', 'spawnvp', 'spawnvpe', 'posix_spawn', 'posix_spawnp'})
PTY_NAMES = frozenset({'spawn', 'fork'})
ASYNCIO_NAMES = frozenset({'create_subprocess_exec', 'create_subprocess_shell'})
NAMESPACES = {'subprocess': SUBPROCESS_NAMES, 'os': OS_NAMES, 'pty': PTY_NAMES,
              'asyncio': ASYNCIO_NAMES}
SPAWN_NAMES = frozenset().union(*NAMESPACES.values())
# Names/attributes that carry the subject (its binary, argv or sandbox) into a
# spawn. A maintenance command may see a path inside the work tree (the
# checkpoint oracle does), but never the subject itself.
SUBJECT_NAMES = frozenset({'hermes', 'hermes_argv', 'plan', 'plans', 'subject',
                           'subjects', 'argv', 'args', 'command', 'entry', 'prompt',
                           'exec_argv', 'double', 'token', 'sandbox_plan'})
SUBJECT_ATTRS = frozenset({'hermes', 'hermes_argv', 'argv', 'plan', 'subject',
                           'entry', 'exec_argv', 'token'})
# The reviewed maintenance table: (relative file, enclosing function, call name).
# Each entry is additionally shape-checked (literal argv, fixed program, a
# checkpoint-script binding), so the table cannot silently cover a new route.
REVIEWED_MAINTENANCE = {
    ('evals/runners/hermes.py', 'skill_tree_from_git', 'run'),
    ('evals/runners/hermes.py', 'bwrap_version', 'run'),
    ('evals/runners/hermes.py', 'repo_head', 'run'),
    ('evals/scripts/casespec.py', '_run_gate', 'run'),
    ('evals/scripts/oracle.py', 'run_checkpoint', 'run'),
}
DRIVER_EVENTS = ['subprocess.Popen', 'subprocess.run', 'os.system', 'os.exec',
                 'os.posix_spawn', 'os.fork', 'os.spawn', 'os.forkpty', 'pty.spawn']


class Site:
    """One discovered spawn call or reference."""

    def __init__(self, path, line, namespace, func, enclosing, argv_expr, kind='call'):
        self.path = path
        self.line = line
        self.namespace = namespace
        self.func = func
        self.enclosing = enclosing
        self.argv_expr = argv_expr
        self.kind = kind

    def rel(self):
        return str(Path(self.path).resolve().relative_to(REPO))

    def __repr__(self):
        return '<%s:%d %s.%s in %s (%s)>' % (self.rel(), self.line, self.namespace,
                                             self.func, self.enclosing, self.kind)


def _import_aliases(tree):
    """local name -> ('subprocess'|'os'|'pty'|'asyncio', real name or None)."""
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for name in node.names:
                top = name.name.split('.')[0]
                if top in NAMESPACES:
                    aliases[name.asname or top] = (top, None)
        elif isinstance(node, ast.ImportFrom) and node.module in NAMESPACES:
            for name in node.names:
                aliases[name.asname or name.name] = (node.module, name.name)
    return aliases


def _attr_chain(node):
    """('subprocess', 'run') for ``subprocess.run`` / ``mod.subprocess.run``."""
    if not isinstance(node, ast.Attribute) or node.attr not in SPAWN_NAMES:
        return None
    base = node.value
    parts = []
    while isinstance(base, ast.Attribute):
        parts.append(base.attr)
        base = base.value
    if isinstance(base, ast.Name):
        parts.append(base.id)
    for part in parts:
        if part in NAMESPACES:
            return (part, node.attr)
    return None


def _binding_map(tree):
    """Name -> (namespace, func) for ``X = subprocess.run`` style aliases."""
    bound = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Attribute):
            resolved = _attr_chain(node.value)
            if resolved:
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        bound[target.id] = resolved
    return bound


def _namespace_of(node, aliases):
    if isinstance(node, ast.Name):
        if node.id in aliases:
            return aliases[node.id][0]
        if node.id in NAMESPACES:
            return node.id
    if isinstance(node, ast.Attribute) and node.attr in NAMESPACES:
        return node.attr
    return None


class _Walker(ast.NodeVisitor):
    def __init__(self, path, aliases, bindings):
        self.path = path
        self.aliases = aliases
        self.bindings = bindings
        self.stack = []
        self.calls = []
        self.refs = []

    def visit_FunctionDef(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _resolve_attr(self, node):
        """Alias-aware attribute: ``_o.system`` with ``import os as _o``.

        R8c: the alias map already covered function-level imports (``ast.walk``),
        but attribute calls were resolved only against the literal namespace
        names, so a one-letter alias hid the spawn.
        """
        if not isinstance(node, ast.Attribute):
            return None
        base = node.value
        while isinstance(base, ast.Attribute):
            base = base.value
        if isinstance(base, ast.Name) and base.id in self.aliases:
            namespace = self.aliases[base.id][0]
            if node.attr in NAMESPACES[namespace]:
                return (namespace, node.attr)
        return None

    def _resolve_call(self, node):
        func = node.func
        if isinstance(func, ast.Attribute):
            return (_attr_chain(func) or self._resolve_attr(func))
        if isinstance(func, ast.Name):
            if func.id in self.bindings:
                return self.bindings[func.id]
            resolved = self.aliases.get(func.id)
            if resolved and resolved[1]:
                return resolved
        return None

    def _resolve_generated(self, node):
        """getattr(subprocess, 'run') / importlib.import_module('subprocess')."""
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr == 'import_module' \
                and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value in NAMESPACES:
            return (node.args[0].value, 'import_module')
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Call):
            # __import__('subprocess').run([...]) (R8c)
            inner = func.value
            iname = inner.func
            if isinstance(iname, ast.Name) and iname.id == '__import__' \
                    and inner.args and isinstance(inner.args[0], ast.Constant) \
                    and inner.args[0].value in NAMESPACES \
                    and func.attr in NAMESPACES[inner.args[0].value]:
                return (inner.args[0].value, func.attr)
        if not isinstance(func, ast.Call):
            return None
        name = func.func
        if isinstance(name, ast.Name) and name.id == 'getattr' and len(func.args) >= 2:
            base = _namespace_of(func.args[0], self.aliases)
            value = func.args[1]
            if base and isinstance(value, ast.Constant) and isinstance(value.value, str):
                return (base, value.value)
        if (isinstance(name, ast.Attribute) and name.attr == 'import_module') \
                or (isinstance(name, ast.Name) and name.id == 'import_module'):
            if func.args and isinstance(func.args[0], ast.Constant) \
                    and func.args[0].value in NAMESPACES:
                return (func.args[0].value, 'import_module')
        return None

    def visit_Call(self, node):
        generated = self._resolve_generated(node)
        if generated:
            namespace, name = generated
            self.refs.append(Site(self.path, node.lineno, namespace, name,
                                  '/'.join(self.stack) or '<module>', None, 'generated'))
            self.generic_visit(node)
            return
        resolved = self._resolve_call(node)
        if resolved:
            namespace, func = resolved
            argv = node.args[0] if node.args else None
            for keyword in node.keywords:
                if keyword.arg in ('args', 'command', 'cmd') and argv is None:
                    argv = keyword.value
            self.calls.append(Site(self.path, node.lineno, namespace, func,
                                   '/'.join(self.stack) or '<module>', argv))
        self.generic_visit(node)

    def visit_Attribute(self, node):
        chain = _attr_chain(node) or self._resolve_attr(node)
        if chain:
            self.refs.append(Site(self.path, node.lineno, chain[0], chain[1],
                                  '/'.join(self.stack) or '<module>', None, 'reference'))
        self.generic_visit(node)


def spawn_sites(path, source=None):
    """(calls, references) discovered in one file."""
    path = Path(path)
    text = source if source is not None else path.read_text(encoding='utf-8')
    tree = ast.parse(text, str(path))
    walker = _Walker(path, _import_aliases(tree), _binding_map(tree))
    walker.visit(tree)
    called = {(site.line, site.func) for site in walker.calls}
    refs = [site for site in walker.refs
            if not (site.kind == 'reference' and (site.line, site.func) in called)]
    return walker.calls, refs


def _subject_references(nodes):
    for node in nodes:
        if isinstance(node, ast.Name) and node.id in SUBJECT_NAMES:
            return True
        if isinstance(node, ast.Attribute) and node.attr in SUBJECT_ATTRS:
            return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and '--hermes' in node.value:
            return True
    return False


def _enclosing_node(tree, line):
    best = None
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, 'end_lineno', node.lineno)
            if node.lineno <= line <= end:
                if best is None or node.lineno > best.lineno:
                    best = node
    return best


def _bound_to_checkpoint(function, name):
    """``name = … 'checkpoint.py'`` inside the function."""
    if function is None:
        return False
    for node in ast.walk(function):
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            for target in getattr(node, 'targets', []):
                if isinstance(target, ast.Name) and target.id == name:
                    constants = {item.value for item in ast.walk(node.value)
                                 if isinstance(item, ast.Constant)
                                 and isinstance(item.value, str)}
                    if any(value.endswith('checkpoint.py') for value in constants):
                        return True
    return False


def _is_executable_attr(node):
    return isinstance(node, ast.Attribute) and node.attr == 'executable'


def _literal_argv(argv, enclosing):
    """The argv shape a reviewed maintenance call may have (mechanical)."""
    if not isinstance(argv, (ast.List, ast.Tuple)):
        return False
    elements = list(argv.elts)
    if not elements:
        return False
    if any(isinstance(item, (ast.Starred, ast.JoinedStr)) for item in elements):
        return False
    if _subject_references(list(ast.walk(argv))):
        return False
    if _is_executable_attr(elements[0]):
        # python <repo script> …: the executed script must be the bound checkpoint.
        if len(elements) < 2 or not isinstance(elements[1], ast.Call):
            return False
        call = elements[1]
        if not (isinstance(call.func, ast.Name) and call.func.id == 'str'
                and call.args and isinstance(call.args[0], ast.Name)):
            return False
        if not _bound_to_checkpoint(enclosing, call.args[0].id):
            return False
        return True
    for item in elements:
        if isinstance(item, ast.Constant) and isinstance(item.value, str):
            continue
        if isinstance(item, ast.Name) and item.id.isupper():
            continue
        if isinstance(item, ast.Attribute) and str(item.attr).isupper():
            continue
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) \
                and item.func.id in ('str', 'Path'):
            continue
        return False
    return True


def classify(site, tree, reviewed=REVIEWED_MAINTENANCE):
    if site.kind != 'call':
        return 'unclassified'
    if Path(site.path).resolve() == HERMES.resolve() and site.func == 'Popen' \
            and site.enclosing == '_popen':
        return 'plan-spawn'
    if (site.rel(), site.enclosing, site.func) not in reviewed:
        return 'unclassified'
    enclosing = _enclosing_node(tree, site.line)
    if enclosing is not None and _subject_references(list(ast.walk(enclosing))):
        return 'unclassified'
    if not _literal_argv(site.argv_expr, enclosing):
        return 'unclassified'
    return 'maintenance'


def scan_tree(path):
    """([(site, kind)] for calls, [(site, 'unclassified')] for references)."""
    text = Path(path).read_text(encoding='utf-8')
    tree = ast.parse(text, str(path))
    calls, refs = spawn_sites(path, text)
    return ([(site, classify(site, tree)) for site in calls],
            [(site, 'unclassified') for site in refs])


def shipped_modules():
    files = sorted(EVALS.rglob('*.py'))
    helper = REPO / 'tests' / 'fake_hermes.py'
    if helper.is_file():
        files.append(helper)
    return [path for path in files if '__pycache__' not in path.parts]


# --------------------------------------------------------------------------- #
# the AST tripwire
# --------------------------------------------------------------------------- #

def test_every_spawn_site_is_classified(capsys):
    offenders = []
    table = []
    for path in shipped_modules():
        calls, refs = scan_tree(path)
        for site, kind in calls + refs:
            table.append('%s:%d %s/%s -> %s' % (
                site.rel(), site.line, site.enclosing, site.func, kind))
            if kind == 'unclassified':
                offenders.append(site)
    with capsys.disabled():
        print('\nspawn inventory:')
        for row in table:
            print('  ' + row)
    assert not offenders, 'unclassified spawn site(s): %r' % (offenders,)


def test_the_runner_owns_the_only_subject_spawn():
    calls, refs = scan_tree(HERMES)
    plans = [site for site, kind in calls if kind == 'plan-spawn']
    assert len(plans) == 1, 'expected one runner-owned spawn API, got %r' % (plans,)
    assert plans[0].func == 'Popen' and plans[0].enclosing == '_popen'
    others = [(site, kind) for site, kind in calls if kind != 'plan-spawn']
    assert others, 'the scanner found no maintenance spawn (vacuous)'
    assert all(kind == 'maintenance' for _site, kind in others), others
    assert not refs, 'unclassified reference(s) in the runner: %r' % (refs,)


def test_run_evals_and_judge_contain_no_spawn():
    """Neither the CLI nor the judge module may name a spawn API at all."""
    for path in (RUN_EVALS, JUDGE):
        text = path.read_text(encoding='utf-8')
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(name.name == 'subprocess' for name in node.names), path.name
            if isinstance(node, ast.ImportFrom):
                assert node.module != 'subprocess', path.name
        calls, refs = scan_tree(path)
        assert calls == [] and refs == [], '%s spawns: %r %r' % (path.name, calls, refs)


def test_the_scanner_flags_every_escape_shape():
    """Non-vacuity: each known escape is discovered and refused."""
    bodies = {
        'os.system': 'import os\ndef go(args):\n    os.system(str(args.hermes))\n',
        'os.popen': 'import os\ndef go():\n    os.popen("x")\n',
        'getstatusoutput': ('import subprocess\ndef go():\n'
                            '    subprocess.getstatusoutput("x")\n'),
        'pty.spawn': 'import pty\ndef go():\n    pty.spawn(["x"])\n',
        'asyncio': ('import asyncio\ndef go():\n'
                    '    asyncio.create_subprocess_exec("x")\n'),
        'getattr': ('import subprocess\ndef go():\n'
                    '    getattr(subprocess, "run")(["x"])\n'),
        'importlib': ('import importlib\ndef go():\n'
                      '    importlib.import_module("subprocess").run(["x"])\n'),
        'alias-assign': ('import subprocess\nRUN = subprocess.run\n'
                         'def go():\n    RUN(["x"])\n'),
        'mod-subprocess': ('import subprocess as x\ndef go():\n'
                           '    x.subprocess.run(["x"])\n'),
        'kwargs': ('import subprocess\ndef go():\n'
                   '    subprocess.run(args=["x"])\n'),
        'starred': ('import subprocess\ndef go(cmd):\n'
                    '    subprocess.run([*cmd])\n'),
        'fstring': ('import subprocess\ndef go(path):\n'
                    '    subprocess.run(["sh", "-c", f"{path}"])\n'),
        'non-call-ref': 'import subprocess\nHANDLER = subprocess.run\n',
        'function-level-alias': ('def go(args):\n    import os as _o\n'
                                 '    _o.system(str(args.hermes))\n'),
        'function-level-from': ('def go():\n    from subprocess import run as r\n'
                                '    r(["x"])\n'),
        'dunder-import': ('def go():\n'
                          "    __import__('subprocess').run(['x'])\n"),
    }
    for name, body in bodies.items():
        calls, refs = spawn_sites('synthetic.py', body)
        kinds = [classify(site, ast.parse(body)) for site in calls]
        assert 'unclassified' in kinds or refs, '%s was not flagged' % name
    assert spawn_sites('synthetic.py', 'def go():\n    return 1\n') == ([], [])
    # Clean controls: the same shapes with a harmless attribute stay silent.
    clean = {
        'function-level-alias': ('def go():\n    import os as _o\n'
                                 '    return _o.getcwd()\n'),
        'function-level-from': ('def go():\n    from subprocess import run as r\n'
                                '    return r.__name__\n'),
        'dunder-import': "def go():\n    return __import__('os').sep\n",
    }
    for name, body in clean.items():
        calls, refs = spawn_sites('synthetic.py', body)
        assert calls == [] and refs == [], (name, calls, refs)


def test_a_reviewed_maintenance_call_only_passes_with_its_shape():
    good = ('import sys, subprocess\nfrom pathlib import Path\n'
            'def run_gate(repo_root, state_path):\n'
            "    script = Path(repo_root) / 'scripts' / 'checkpoint.py'\n"
            "    return subprocess.run([sys.executable, str(script), 'gate',"
            " str(state_path)])\n")
    bad_program = ('import subprocess, sys\ndef run_gate(target):\n'
                   '    return subprocess.run([sys.executable, target])\n')
    bad_shell = ('import subprocess\ndef run_gate(path):\n'
                 '    return subprocess.run(["sh", "-c", path])\n')
    reviewed = {('synthetic.py', 'run_gate', 'run')}
    for body, want in ((good, 'maintenance'), (bad_program, 'unclassified'),
                       (bad_shell, 'unclassified')):
        calls, refs = spawn_sites('synthetic.py', body)
        got = [classify(site, ast.parse(body), reviewed) for site in calls]
        assert got == [want], (body, got, refs)


# --------------------------------------------------------------------------- #
# the structural proof: a handler that accepts --hermes must call the gate
# before it runs anything else (R8c; independent of every name catalogue)
# --------------------------------------------------------------------------- #

# Every call a subject-bearing handler may make before ``offline_gate``. One line
# of justification per entry; anything else fails, whatever its alias, argument
# condition or spelling. A new call means a new review.
ALLOWED_PRE_GATE = {
    'print': 'writes to stdout',
    'getattr': 'reads an attribute; calls nothing',
    'str': 'conversion', 'int': 'conversion', 'len': 'conversion',
    'bool': 'conversion', 'list': 'conversion', 'tuple': 'conversion',
    'dict': 'conversion', 'set': 'conversion', 'sorted': 'conversion',
    'isinstance': 'type test',
    'Path': 'path construction',
    'Path.home': 'path construction',
    'root.mkdir': 'creates the output directory',
    'os.environ.get': 'reads one environment variable',
    'now_iso': 'timestamp string',
    'hermes_argv': 'splits --hermes with shlex; starts nothing',
    'load_hermes': 'imports the runner module; its Popen sits behind _popen',
    'load_casespec': 'imports the case module',
    'load_oracle': 'imports the oracle module; its gate call sits behind run_checkpoint',
    'cleanup': 'reaps ledger-owned directories and signals; starts nothing',
    'cmd_probe': 'a sibling handler this same test proves',
    'cmd_pilot': 'a sibling handler this same test proves',
    'hermes.scrub_env': 'filters environment keys',
}


def _dotted(func):
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        parts = [func.attr]
        base = func.value
        while isinstance(base, ast.Attribute):
            parts.append(base.attr)
            base = base.value
        if isinstance(base, ast.Name):
            parts.append(base.id)
            return '.'.join(reversed(parts))
        return '(...).' + func.attr
    return '<expr>'


def _module_functions(tree):
    return {node.name: node for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _uses_subject(function, functions, seen=None):
    """``args.hermes`` is reachable from this handler, directly or through a local call."""
    seen = set() if seen is None else seen
    if function.name in seen:
        return False
    seen.add(function.name)
    for node in ast.walk(function):
        if isinstance(node, ast.Attribute) and node.attr == 'hermes' \
                and isinstance(node.value, ast.Name) and node.value.id == 'args':
            return True
        if isinstance(node, ast.Constant) and node.value == '--hermes':
            return True
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in functions \
                and _uses_subject(functions[node.func.id], functions, seen):
            return True
    return False


def _parser_subcommands(tree):
    builder = next(node for node in tree.body
                   if isinstance(node, ast.FunctionDef) and node.name == 'build_parser')
    names = set()
    for node in ast.walk(builder):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                and node.func.attr == 'add_parser':
            names.update(arg.value for arg in node.args
                         if isinstance(arg, ast.Constant) and isinstance(arg.value, str))
        if isinstance(node, ast.For) and isinstance(node.iter, (ast.Tuple, ast.List)):
            names.update(item.value for item in node.iter.elts
                         if isinstance(item, ast.Constant) and isinstance(item.value, str))
    return names


def _dispatch_handlers(tree):
    main = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == 'main')
    for node in ast.walk(main):
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict) \
                and any(isinstance(target, ast.Name) and target.id == 'handlers'
                        for target in node.targets):
            return {key.value: value.id
                    for key, value in zip(node.value.keys, node.value.values)
                    if isinstance(key, ast.Constant) and isinstance(key.value, str)
                    and isinstance(value, ast.Name)}
    return {}


def _gate_call(function):
    """The single ``offline_gate(...)`` call of a handler."""
    gates = [node for node in ast.walk(function) if isinstance(node, ast.Call)
             and _dotted(node.func) == 'offline_gate']
    assert gates, '%s never calls offline_gate' % function.name
    assert len(gates) == 1, '%s calls offline_gate %d times' % (function.name, len(gates))
    return gates[0]


def _contains_gate(node, gate):
    return any(child is gate for child in ast.walk(node))


def _calls_in(node, tree, function, skip=()):
    """Calls in ``node`` evaluated in the handler's frame, found structurally.

    A nested ``def``/``lambda`` contributes only what runs when the statement
    itself runs: its decorators, default values and annotations (evaluated at
    definition time, on the ``def`` line but in THIS frame). Its body runs later,
    if at all.
    R8d: this replaces a line-number test that took the ``def`` line, defaults
    included, for the nested body.
    """
    out = []

    def visit(current):
        if isinstance(current, ast.Call) and not any(current is item for item in skip):
            out.append(current)
        if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            arguments = current.args
            every_argument = (arguments.posonlyargs + arguments.args + arguments.kwonlyargs
                              + [a for a in (arguments.vararg, arguments.kwarg) if a])
            evaluated = (list(getattr(current, 'decorator_list', [])) + list(arguments.defaults)
                         + [d for d in arguments.kw_defaults if d]
                         + [a.annotation for a in every_argument if a.annotation]
                         + [r for r in (getattr(current, 'returns', None),) if r])
            for part in evaluated:
                visit(part)
            return
        for child in ast.iter_child_nodes(current):
            visit(child)

    visit(node)
    return out


def _pre_gate_calls(statements, gate, tree, function):
    """Every call this handler evaluates before ``gate``, in execution order.

    R8d: evaluation order, not line numbers. A statement that contains the gate
    contributes its header (``if``/``while`` test, ``for`` iter, ``with`` items)
    and -- only when the gate is in its BODY -- the statements before the gate. A
    simple statement contributes every call except the gate itself, so the gate's
    arguments, the other operand of ``or``/``and`` and conditional expressions are
    covered. A body that runs after the gate (``if not offline_gate(...):``) is not
    examined.
    """
    out = []
    for stmt in statements:
        if not _contains_gate(stmt, gate):
            if stmt.lineno <= gate.lineno:
                out.extend(_calls_in(stmt, tree, function))
            continue
        if isinstance(stmt, (ast.If, ast.While)):
            header, body = [stmt.test], stmt.body
        elif isinstance(stmt, (ast.For, ast.AsyncFor)):
            header, body = [stmt.iter, stmt.target], stmt.body
        elif isinstance(stmt, (ast.With, ast.AsyncWith)):
            header = ([item.context_expr for item in stmt.items]
                      + [item.optional_vars for item in stmt.items if item.optional_vars])
            body = stmt.body
        elif isinstance(stmt, ast.Try):
            header, body = [], stmt.body
        else:
            header, body = [], None
        for part in header:
            out.extend(_calls_in(part, tree, function, skip=(gate,)))
        if body is None:
            out.extend(_calls_in(stmt, tree, function, skip=(gate,)))
        elif not any(_contains_gate(part, gate) for part in header):
            out.extend(_pre_gate_calls(body, gate, tree, function))
        return out
    return out


def test_the_gate_rule_follows_evaluation_order():
    """R8d OA1/OA3: the gate statement is only safe if its own calls are pure.

    A line-number comparison cannot see a spawn that shares the gate's line or
    hides in the gate's argument; evaluation order can.
    """

    def violations(body):
        source = 'def cmd_run(args, evals_dir, root):\n' + body
        tree = ast.parse(source)
        function = _module_functions(tree)['cmd_run']
        gate = _gate_call(function)
        return [(node.lineno, _dotted(node.func))
                for node in _pre_gate_calls(function.body, gate, tree, function)]

    bad = {
        'or-operand': ("    token = (getattr(args, 'max_attempts', 1) == 9 "
                       "and os.popen('true')) or offline_gate(args)\n"
                       "    return token\n"),
        'gate-argument': ("    token = offline_gate(\n"
                          "        args if getattr(args, 'max_attempts', 1) != 9 "
                          "else os.popen('true'))\n"
                          "    return token\n"),
        'if-header': ("    if os.system('true') or offline_gate(args):\n"
                      "        return 1\n    return 0\n"),
        'before-the-gate': ("    os.system('true')\n"
                            "    token = offline_gate(args)\n    return token\n"),
        # a default value is evaluated when the ``def`` statement runs, not when the
        # function is called: the call sits on the ``def`` line but runs in this frame
        'nested-default': ("    def helper(x=(os.popen('true') if getattr(args, 'max_attempts', 1) == 9 "
                           "else None)):\n        return x\n"
                           "    token = offline_gate(args)\n    return token\n"),
        'nested-kw-default': ("    def helper(*, x=(os.popen('true') if getattr(args, 'max_attempts', 1) == 9 "
                              "else None)):\n        return x\n"
                              "    token = offline_gate(args)\n    return token\n"),
        # annotations are evaluated when the ``def`` runs too (no ``from __future__ import annotations``)
        'nested-annotation': ("    def helper(x: (os.popen('true') if getattr(args, 'max_attempts', 1) == 9 "
                              "else None) = None):\n        return x\n"
                              "    token = offline_gate(args)\n    return token\n"),
        'nested-return-annotation': ("    def helper() -> (os.popen('true') if getattr(args, 'max_attempts', 1) == 9 "
                                     "else None):\n        return None\n"
                                     "    token = offline_gate(args)\n    return token\n"),
        'lambda-default': ("    helper = lambda x=(os.popen('true') if getattr(args, 'max_attempts', 1) == 9 "
                           "else None): x\n"
                           "    token = offline_gate(args)\n    return token\n"),
    }
    for label, body in bad.items():
        got = violations(body)
        assert got, '%s was accepted as a pre-gate shape' % label
        assert any(name not in ALLOWED_PRE_GATE for _line, name in got), (label, got)

    clean = {
        'if-header-allowlisted': ("    if getattr(args, 'hermes', None) and "
                                  "offline_gate(args):\n        return 1\n    return 0\n"),
        'body-after-the-gate': ("    if not offline_gate(args):\n"
                                "        os.system('runs after the gate')\n"
                                "        return 2\n    return 0\n"),
        'plain': "    token = offline_gate(args)\n    return token\n",
        # the body of a nested def runs only if it is called; an allow-listed default is harmless
        'nested-body-not-run': ("    def helper(x=getattr(args, 'hermes', None)):\n"
                                "        return os.system('never called before the gate')\n"
                                "    token = offline_gate(args)\n    return token\n"),
    }
    for label, body in clean.items():
        got = violations(body)
        assert all(name in ALLOWED_PRE_GATE for _line, name in got), (label, got)


def test_every_cli_handler_gates_before_any_other_call(capsys):
    """IN8/IN11A/IN11B guard: a spawn before the gate is red whatever its spelling.

    The handlers come from the parser and its dispatch table, not from a list in
    this test; the discovered subject-bearing set is pinned so a new handler
    cannot be skipped silently. R8d: the check follows evaluation order, not line
    numbers, so a spawn sharing the gate's line or hiding in its argument is red.
    """
    tree = ast.parse(RUN_EVALS.read_text(encoding='utf-8'))
    functions = _module_functions(tree)
    handlers = _dispatch_handlers(tree)
    commands = _parser_subcommands(tree)
    assert commands, 'the parser declared no subcommand'
    assert set(handlers) == commands, (sorted(commands), sorted(handlers))
    assert all(name in functions for name in handlers.values()), handlers
    # Every function that owns a gate is in scope, discovered -- not just the
    # dispatch targets: cmd_probe/cmd_pilot are entered from cmd_run.
    gated = {name for name, function in functions.items()
             if any(isinstance(node, ast.Call) and _dotted(node.func) == 'offline_gate'
                    for node in ast.walk(function))}
    assert gated == {'cmd_run', 'cmd_measure', 'cmd_probe', 'cmd_pilot'}, sorted(gated)
    assert {'cmd_run', 'cmd_measure'} <= gated <= set(functions)
    table = []
    for name in sorted(gated):
        function = functions[name]
        gate = _gate_call(function)
        labels = [(node.lineno, _dotted(node.func))
                  for node in _pre_gate_calls(function.body, gate, tree, function)]
        table.extend('%s:%d %s' % (name, line, label) for line, label in labels)
        early = [(line, label) for line, label in labels
                 if label not in ALLOWED_PRE_GATE]
        assert not early, ('%s evaluates %r before offline_gate() at line %d; only the '
                           'reviewed pre-gate calls are allowed'
                           % (name, early, gate.lineno))
    with capsys.disabled():
        print('\npre-gate calls (all allow-listed):')
        for row in table:
            print('  ' + row)
    assert table, 'no pre-gate call was examined (vacuous)'


# --------------------------------------------------------------------------- #
# the CLI: every route must reach the gate before it spawns (runtime proof)
# --------------------------------------------------------------------------- #

RUNTIME_DRIVER = r'''
import argparse, contextlib, importlib.util, io, json, sys
from pathlib import Path
EVENTS = []
def hook(event, args):
    if event in %(events)r:
        try:
            payload = [str(item)[:400] for item in args]
        except Exception:
            payload = ['<unprintable>']
        EVENTS.append([event, payload])
sys.addaudithook(hook)
repo = Path(sys.argv[1]); state = Path(sys.argv[2]); out = Path(sys.argv[3])
result_path = Path(sys.argv[4])
spec = importlib.util.spec_from_file_location(
    'lheval_run_evals', repo / 'evals' / 'scripts' / 'run_evals.py')
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod
spec.loader.exec_module(mod)
parser = mod.build_parser()
groups = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
'''


def _run_driver(tmp_path):
    driver = tmp_path / 'runtime_routes.py'
    source = (RUNTIME_DRIVER % {'events': DRIVER_EVENTS}) + '''\
results = []
base = ['--out-root', str(out), '--run-id', 'inv', '--trials', '1',
        '--max-attempts', '1', '--parallel', '1', '--hermes', '/bin/true',
        '--offline-state', str(state)]
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    for name, child in sorted(groups[0].choices.items()):
        variants = [None]
        for action in child._actions:
            if not action.option_strings or action.dest in (
                    'command', 'hermes', 'offline_state', 'out_root'):
                continue
            if isinstance(action, argparse._StoreTrueAction):
                variants.extend([[opt] for opt in action.option_strings])
                continue
            default = action.default
            if isinstance(default, bool) or default is None:
                continue
            if isinstance(default, int):
                variants.append([action.option_strings[0], str(default + 1)])
            elif isinstance(default, str):
                variants.append([action.option_strings[0], default + '-alt'])
        for variant in variants:
            variant = variant or []
            argv = [name] + base + variant
            EVENTS.clear()
            try:
                rc = mod.main(argv)
                error = ''
            except BaseException as exc:
                rc = 'raised'
                error = type(exc).__name__ + ': ' + str(exc)[:200]
            results.append({'route': ' '.join([name] + variant),
                            'rc': rc, 'error': error,
                            'events': [list(item) for item in EVENTS]})
result_path.write_text(json.dumps(results), encoding='utf-8')
'''
    driver.write_text(source, encoding='utf-8')
    state = tmp_path / 'state'
    state.mkdir()
    out = tmp_path / 'out'
    home = tmp_path / 'home'
    home.mkdir()
    result = tmp_path / 'routes.json'
    env = {'HOME': str(home), 'PATH': os.environ.get('PATH', '/usr/bin'),
           'LANG': 'C.UTF-8', 'TMPDIR': str(tmp_path), 'PYTHONDONTWRITEBYTECODE': '1'}
    proc = subprocess.run([sys.executable, str(driver), str(REPO), str(state), str(out),
                           str(result)], capture_output=True, text=True, env=env,
                          timeout=300)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(result.read_text(encoding='utf-8'))


def test_no_cli_route_spawns_before_the_gate(tmp_path):
    """IN8 guard: a host spawn on any route before the gate turns this red."""
    results = _run_driver(tmp_path)
    assert results, 'the driver discovered no route'
    routes = [item['route'] for item in results]
    assert 'run' in routes and 'measure' in routes, routes
    assert 'measure --no-judge' in routes, routes
    gated = [item for item in results if item['rc'] in (2, 3)]
    assert gated, 'no discovered route reached the gate'
    for item in gated:
        assert not item['events'], (
            '%s spawned before the gate: %r' % (item['route'], item['events']))
    maintenance = []
    for item in results:
        if item['rc'] in (2, 3):
            continue
        for event, payload in item['events']:
            joined = ' '.join(payload)
            assert 'scripts/checkpoint.py' in joined and '--hermes' not in joined, (
                '%s spawned something that is not the checkpoint oracle: %r'
                % (item['route'], [event, payload]))
            maintenance.append(event)
    assert maintenance, 'the audit hook recorded no maintenance spawn (vacuous)'
