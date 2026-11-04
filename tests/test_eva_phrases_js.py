"""web/app.js has a JavaScript copy of voice/wake_phrases.py: it must agree on every case.

Runs the JS with node, or macOS's built-in JavaScript (osascript); skipped when neither exists.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from tests import test_eva_phrases as py  # module import: its tests aren't collected twice

_APP = Path(__file__).resolve().parents[1] / "web" / "app.js"


def _cases(fn) -> list:
    mark = next(m for m in fn.pytestmark if m.name == "parametrize")
    return list(mark.args[1])


def _js_block() -> str:
    src = _APP.read_text(encoding="utf-8")
    start = src.index("// ── Єва: «Єва, скажи» wakes her")
    end = src.index("// mode: off →", start)
    return src[start:end]


def _run_js(code: str) -> str:
    if shutil.which("node"):
        return subprocess.run(["node", "-e", code + "\nconsole.log(OUT);"], capture_output=True, text=True, check=True).stdout
    if shutil.which("osascript"):
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8") as f:
            f.write(code + "\nOUT;")
        return subprocess.run(["osascript", "-l", "JavaScript", f.name], capture_output=True, text=True, check=True).stdout
    pytest.skip("no JavaScript runtime (node / osascript)")


def test_js_matches_python_rules():
    checks = {
        "wake": [[text, rest] for text, rest in _cases(py.test_wake_variants)],
        "not_wake": _cases(py.test_not_a_wake),
        "stop": _cases(py.test_stop_variants),
        "not_stop": _cases(py.test_not_a_stop),
    }
    code = _js_block().replace("const ", "var ") + f"""
var C = {json.dumps(checks, ensure_ascii=False)};
var bad = [];
C.wake.forEach(function (c) {{ if (matchWake(c[0]) !== c[1]) bad.push("wake " + c[0] + " -> " + matchWake(c[0])); }});
C.not_wake.forEach(function (t) {{ if (matchWake(t) !== null) bad.push("not_wake " + t); }});
C.stop.forEach(function (t) {{ if (!isStop(t)) bad.push("stop " + t); }});
C.not_stop.forEach(function (t) {{ if (isStop(t)) bad.push("not_stop " + t); }});
var OUT = JSON.stringify(bad);
"""
    assert json.loads(_run_js(code).strip()) == []
