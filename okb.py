"""Load the original Open Korean Bible JSON and index it for display."""
import argparse
import json
import os
import sys
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.request import urlopen

from import_bible import CODES

ROOT = Path(__file__).resolve().parent
FILENAME = ROOT / "bible_all.json"
REVISION = "8428e604a0785a43d667dad2d0410a3999f2c879"
DOWNLOAD = f"https://raw.githubusercontent.com/open-korean-bible/open-korean-bible/{REVISION}/data/bible_all.json"
MAX_BYTES = 24 * 1024 * 1024


def index_books(document):
    result = {}
    for book in document["books"]:
        number = int(book["book_number"])
        if number not in range(1, len(CODES) + 1) or CODES[number - 1] in result:
            raise ValueError("권 번호가 중복되었거나 범위를 벗어났습니다.")
        chapters = {}
        for chapter in book["chapters"]:
            chapter_number = str(int(chapter["chapter"]))
            verses = {}
            for verse in chapter["verses"]:
                text = verse["original"]
                if not isinstance(text, str):
                    raise ValueError("원문 본문 형식이 올바르지 않습니다.")
                verses[str(int(verse["verse"]))] = text
            if verses:
                chapters[chapter_number] = verses
        if chapters:
            result[CODES[number - 1]] = chapters
    if len(result) != len(CODES):
        raise ValueError("구약·신약 66권이 들어 있는 합본 JSON이 필요합니다.")
    return result


def main():
    parser = argparse.ArgumentParser(description="Open Korean Bible 전권 데이터 설치")
    parser.add_argument("file", nargs="?", type=Path, help="이미 받은 bible_all.json")
    args = parser.parse_args()
    try:
        if args.file:
            with args.file.open("rb") as source:
                raw = source.read(MAX_BYTES + 1)
        else:
            with urlopen(DOWNLOAD, timeout=30) as response:
                raw = response.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("데이터가 지원 용량을 초과합니다.")
        document = json.loads(raw)
        books = index_books(document)
        for book in document["books"]:
            for chapter in book["chapters"]:
                chapter["verses"] = [
                    {"verse": verse["verse"], "original": verse["original"]}
                    for verse in chapter["verses"]
                ]
        count = sum(len(verses) for chapters in books.values() for verses in chapters.values())
        with NamedTemporaryFile(dir=ROOT, delete=False) as temp:
            name = temp.name
            temp.write(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        try:
            os.replace(name, FILENAME)
        finally:
            Path(name).unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        sys.exit(f"Open Korean Bible 파일을 설치하지 못했습니다. {error}")
    print(f"Open Korean Bible {len(books)}권, {count:,}절 설치 완료")


if __name__ == "__main__":
    main()
