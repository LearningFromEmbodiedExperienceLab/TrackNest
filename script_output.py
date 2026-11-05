"""Language and terminal colors for the lifecycle scripts."""
import argparse
import atexit
import os
import sys


class Console:
    def __init__(self, language):
        self.language = language

    def text(self, english, chinese):
        return chinese if self.language == "zh" else english

    def say(self, english, chinese, kind="info"):
        stream = sys.stderr if kind == "error" else sys.stdout
        message = self.text(english, chinese)
        if stream.isatty() and "NO_COLOR" not in os.environ:
            color = {"info": "36", "success": "32", "warning": "33", "error": "31"}[kind]
            message = f"\033[{color}m{message}\033[0m"
        print(message, file=stream, flush=True)

    def fail(self, english, chinese):
        self.say(english, chinese, "error")
        raise SystemExit(1)


def script_parser(name, english, chinese):
    atexit.register(print, flush=True)
    language_parser = argparse.ArgumentParser(add_help=False)
    language_parser.add_argument("--lang", choices=("en", "zh"), default="en")
    language, _ = language_parser.parse_known_args()
    console = Console(language.lang)
    parser = argparse.ArgumentParser(prog=name, description=console.text(english, chinese))
    parser.add_argument("--lang", choices=("en", "zh"), default="en",
                        help=console.text("output language (default: en)", "输出语言（默认：en）"))
    return parser, console
