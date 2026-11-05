"""Verify lifecycle message language and colors without operating services."""
import io

import pytest

from script_output import Console


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("language,expected", [("en", "Running"), ("zh", "运行中")])
@pytest.mark.parametrize("kind,color", [("info", "36"), ("success", "32"), ("warning", "33"), ("error", "31")])
def test_terminal_colors_and_language(monkeypatch, language, expected, kind, color):
    monkeypatch.delenv("NO_COLOR", raising=False)
    stdout, stderr = Terminal(), Terminal()
    monkeypatch.setattr("sys.stdout", stdout)
    monkeypatch.setattr("sys.stderr", stderr)
    Console(language).say("Running", "运行中", kind)
    stream = stderr if kind == "error" else stdout
    assert stream.getvalue() == f"\033[{color}m{expected}\033[0m\n"
    assert (stdout if kind == "error" else stderr).getvalue() == ""


def test_redirected_output_and_no_color(monkeypatch):
    redirected = io.StringIO()
    monkeypatch.setattr("sys.stdout", redirected)
    Console("en").say("Stopped", "已停止", "warning")
    assert redirected.getvalue() == "Stopped\n"
    terminal = Terminal()
    monkeypatch.setattr("sys.stderr", terminal)
    monkeypatch.setenv("NO_COLOR", "1")
    with pytest.raises(SystemExit) as error:
        Console("zh").fail("Failed", "失败")
    assert error.value.code == 1
    assert terminal.getvalue() == "失败\n"
