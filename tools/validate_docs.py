"""Check local inline Markdown links and heading anchors in tracked guides.

External URLs and code fences are excluded; this does not validate CLI behavior.
Run from any directory with the checkout's Python interpreter.
"""
from pathlib import Path
import re
import subprocess
from urllib.parse import unquote


def main():
    root = Path(__file__).resolve().parents[1]
    files = [root / name for name in subprocess.check_output(
        ["git", "ls-files", "*.md"], cwd=root, text=True).splitlines()
        if (root / name).is_file()]
    errors, checked = [], 0
    for source in files:
        content = re.sub(r"```.*?```", "", source.read_text(encoding="utf-8"), flags=re.S)
        for match in re.finditer(r'\[[^\]]*\]\(([^\s)]+)(?:\s+"[^"]*")?\)', content):
            url = match.group(1).strip("<>")
            if re.match(r"[a-zA-Z][\w+.-]*:", url):
                continue
            name, _, anchor = url.partition("#")
            target = (source.parent / unquote(name)).resolve() if name else source
            checked += 1
            if not target.exists():
                errors.append(f"{source.relative_to(root)}: missing {url}")
            elif anchor and target.suffix == ".md":
                headings = re.findall(r"^#{1,6}\s+(.+?)\s*#*$",
                                      target.read_text(encoding="utf-8"), re.M)
                slugs = [re.sub(r"[^\w\- ]", "", h.lower()).replace(" ", "-") for h in headings]
                if unquote(anchor) not in slugs:
                    errors.append(f"{source.relative_to(root)}: missing anchor {url}")
    print(f"{len(files)} Markdown files, {checked} local links; {len(errors)} errors")
    for error in errors:
        print(error)
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
