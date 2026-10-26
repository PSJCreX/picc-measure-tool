"""docs/guide.src.html → docs/guide.html 빌드.

아티팩트는 외부 호스트를 부를 수 없다(Google Fonts만 예외). 사이트 전반과 같은
Freesentation 서체를 쓰려면 woff2를 data URI로 인라인해야 해서, 소스에 남겨 둔
`/*__FREESENTATION__*/` 자리에 @font-face 블록을 채워 넣는다.

    .venv\\Scripts\\python.exe docs\\build_guide.py
"""
from __future__ import annotations

import base64
import sys
from pathlib import Path

DOCS = Path(__file__).resolve().parent
FONTS = DOCS.parent / "static" / "fonts"
SRC = DOCS / "guide.src.html"
OUT = DOCS / "guide.html"

# 문서에서 실제로 쓰는 굵기만 넣는다 — 안 쓰는 굵기까지 넣으면 파일만 커진다
WEIGHTS = {
    400: "Freesentation-4Regular.woff2",
    500: "Freesentation-5Medium.woff2",
    700: "Freesentation-7Bold.woff2",
    800: "Freesentation-8ExtraBold.woff2",
}

PLACEHOLDER = "/*__FREESENTATION__*/"


def font_face_css() -> str:
    blocks = []
    for weight, filename in WEIGHTS.items():
        path = FONTS / filename
        if not path.exists():
            raise SystemExit(f"폰트 파일이 없습니다: {path}")
        b64 = base64.b64encode(path.read_bytes()).decode("ascii")
        blocks.append(
            "@font-face{font-family:'Freesentation';"
            f"src:url(data:font/woff2;base64,{b64}) format('woff2');"
            f"font-weight:{weight};font-style:normal;font-display:swap;}}"
        )
    return "\n".join(blocks)


def main() -> int:
    if not SRC.exists():
        raise SystemExit(f"소스가 없습니다: {SRC}")
    html = SRC.read_text(encoding="utf-8")
    if PLACEHOLDER not in html:
        raise SystemExit(f"자리표시자 {PLACEHOLDER} 를 찾을 수 없습니다.")

    html = html.replace(PLACEHOLDER, font_face_css(), 1)
    OUT.write_text(html, encoding="utf-8")

    kb = OUT.stat().st_size / 1024
    print(f"빌드 완료: {OUT}  ({kb:,.0f} KB, 폰트 {len(WEIGHTS)}종 인라인)")
    if kb > 16 * 1024:
        print("경고: 아티팩트 상한 16MB를 넘었습니다.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
