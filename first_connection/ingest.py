"""Allowlisted public-page snapshots. No login, recursive crawl or form submission."""
import hashlib
import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

ROOT = Path(__file__).resolve().parents[1]
USER_AGENT = "FirstConnectionResearch/0.1 (public welfare research; manual source allowlist)"
LIMIT = 3_000_000


class ReadableText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.skip = 0
        self.parts = []

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "tr", "td", "h1", "h2", "h3", "h4"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        return "\n".join(x for p in "".join(self.parts).splitlines() if (x := re.sub(r"\s+", " ", p).strip()))


def download(url):
    with urlopen(Request(url, headers={"User-Agent": USER_AGENT}), timeout=20) as response:
        if urlsplit(response.url).netloc != urlsplit(url).netloc:
            raise ValueError("다른 호스트로 이동하는 URL은 수집하지 않음")
        data = response.read(LIMIT + 1)
        if len(data) > LIMIT:
            raise ValueError("페이지 크기 제한 초과")
        return data, response.headers.get_content_charset() or "utf-8", response.status


def crawl(root=ROOT):
    sources = json.loads((root / "data/sources.json").read_text())
    raw = root / "data/raw"
    raw.mkdir(parents=True, exist_ok=True)
    manifest, robots = [], {}
    for source in sources:
        row = dict(source, collected_at=datetime.now(timezone.utc).isoformat(), status="error")
        try:
            parts = urlsplit(source["url"])
            origin = f"{parts.scheme}://{parts.netloc}"
            if origin not in robots:
                try:
                    body, encoding, _ = download(origin + "/robots.txt")
                    policy = body.decode(encoding, "replace")
                    rp = RobotFileParser()
                    rp.parse(policy.splitlines())
                    (raw / f"robots_{parts.netloc}.txt").write_text(policy)
                    robots[origin] = rp
                except HTTPError as exc:
                    if exc.code != 404:
                        raise
                    rp = RobotFileParser()
                    rp.parse([])
                    robots[origin] = rp
            if not robots[origin].can_fetch(USER_AGENT, source["url"]):
                raise ValueError("robots.txt에서 허용하지 않은 경로")
            data, encoding, status = download(source["url"])
            parser = ReadableText()
            parser.feed(data.decode(encoding, "replace"))
            digest = hashlib.sha256(data).hexdigest()
            version_dir = raw / source['id']
            version_dir.mkdir(parents=True, exist_ok=True)
            html_path = version_dir / f"{digest}.html"
            text_path = version_dir / f"{digest}.txt"
            html_path.write_bytes(data)
            readable = parser.text()
            text_path.write_text(readable, encoding="utf-8")
            row.update(status="collected", http_status=status, sha256=digest, encoding=encoding,
                       raw_path=str(html_path.relative_to(root)), text_path=str(text_path.relative_to(root)),
                       reuse_status="공식 무료 공개 페이지; 개별 이용조건 검토 필요; 대회 제출 검토용 원본 보존")
            if "제4유형" in readable:
                row["reuse_status"] = "공공누리 제4유형: 출처표시·상업적 이용금지·변경금지. 참고자료로만 보존하고 추천·LLM 입력에서 제외."
        except Exception as exc:
            row["error"] = str(exc)
        manifest.append(row)
    (root / "data/manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    archive_dir = raw / "manifests"
    archive_dir.mkdir(exist_ok=True)
    (archive_dir / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")).write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    return manifest


if __name__ == "__main__":
    for entry in crawl():
        print(entry["id"], entry["status"], entry.get("error", ""))
