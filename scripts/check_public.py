"""Offline release guard. Report locations/categories only, never matched values."""

import re
import subprocess
from pathlib import Path

RULES = {
    "personal_home": re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s\"']+", re.I),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    "credential_literal": re.compile(
        r"(?:ALPACA_KEY|ALPACA_SECRET|api_secret|secret_key)\s*[=:]\s*[\"']?[A-Za-z0-9_\-/+]{20,}",
        re.I,
    ),
    "account_literal": re.compile(
        r"[\"']?(?:account_id|acc_id|moomoo_id)[\"']?\s*[:=]\s*[\"']?(?:\d{6,}|[0-9a-f]{8}-[0-9a-f-]{27,})",
        re.I,
    ),
}


def findings(name, raw):
    path = Path(name)
    if (
        path.name == ".env"
        or path.suffix in {".pem", ".key", ".pfx", ".p12"}
        or path.parts[0] in {"state", "logs", "backups", ".venv", ".venv-brokers"}
        or name.startswith(".env.")
        and name != ".env.example"
    ):
        yield (name, 0, "private_file")
    if path.suffix.lower() in {".png", ".jpg", ".gif", ".ico"}:
        return  # Public images must separately be reviewed as synthetic.
    for line, value in enumerate(raw.decode("utf-8", errors="replace").splitlines(), 1):
        for category, pattern in RULES.items():
            if pattern.search(value):
                yield (name, line, category)


def main():
    root = Path(__file__).resolve().parents[1]
    names = subprocess.check_output(["git", "ls-files", "-z"], cwd=root).decode().split("\0")
    errors = []
    for name in filter(None, names):
        path = root / name
        if path.is_file():
            errors.extend(findings(name, path.read_bytes()))
    for name, line, category in errors:
        print(f"BLOCK {name}:{line} [{category}]")
    print(f"Public-tree privacy checks: {len(errors)} findings (values withheld)")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(main())
