"""Import an eBible.org VPL zip into a local translation JSON file."""
import json
import re
import sys
import zipfile

CODES = """GEN EXO LEV NUM DEU JOS JDG RUT 1SA 2SA 1KI 2KI 1CH 2CH EZR NEH EST JOB PSA PRO ECC SNG ISA JER LAM EZK DAN HOS JOL AMO OBA JON MIC NAM HAB ZEP HAG ZEC MAL MAT MRK LUK JHN ACT ROM 1CO 2CO GAL EPH PHP COL 1TH 2TH 1TI 2TI TIT PHM HEB JAS 1PE 2PE 1JN 2JN 3JN JUD REV""".split()
NAMES = """창세기 출애굽기 레위기 민수기 신명기 여호수아 사사기 룻기 사무엘상 사무엘하 열왕기상 열왕기하 역대상 역대하 에스라 느헤미야 에스더 욥기 시편 잠언 전도서 아가 이사야 예레미야 예레미야애가 에스겔 다니엘 호세아 요엘 아모스 오바댜 요나 미가 나훔 하박국 스바냐 학개 스가랴 말라기 마태복음 마가복음 누가복음 요한복음 사도행전 로마서 고린도전서 고린도후서 갈라디아서 에베소서 빌립보서 골로새서 데살로니가전서 데살로니가후서 디모데전서 디모데후서 디도서 빌레몬서 히브리서 야고보서 베드로전서 베드로후서 요한일서 요한이서 요한삼서 유다서 요한계시록""".split()
CHAPTERS = [50,40,27,36,34,24,21,4,31,24,22,25,29,36,10,13,10,42,150,31,12,8,66,52,5,48,12,14,3,9,1,4,7,3,9,3,2,14,4,28,16,24,21,28,16,16,13,6,6,4,4,5,3,6,4,3,1,13,5,5,3,5,1,1,1,22]
CHAPTERS[33:39] = [3,3,3,2,14,4]


if __name__ == "__main__":
    with zipfile.ZipFile(sys.argv[1]) as archive:
        candidates = [n for n in archive.namelist() if n.lower().endswith("_vpl.txt")]
        if not candidates:
            raise SystemExit("아카이브에 VPL 텍스트가 없습니다.")
        lines = archive.read(candidates[0]).decode("utf-8-sig").splitlines()
    data = {}
    for line in lines:
        match = re.match(r"([A-Z0-9]{3})\s+(\d+):(\d+)\s+(.+)", line)
        if match:
            book, chapter, verse, passage = match.groups()
            if book in CODES:
                data.setdefault(book, {}).setdefault(chapter, {})[verse] = passage
    if len(data) != 66:
        raise SystemExit(f"66권이 필요합니다. 읽은 권 수: {len(data)}")
    with open(sys.argv[2] if len(sys.argv) > 2 else "kor.json", "w", encoding="utf-8") as output:
        json.dump(data, output, ensure_ascii=False)
