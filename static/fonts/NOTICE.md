# Bundled fonts

`Freesentation-*.woff2` — **Freesentation**, by The Freesentation Authors
(<https://freesentation.blog/>), released under the
**SIL Open Font License, Version 1.1**.

The full licence text, as distributed with the font, is in
[`OFL.txt`](OFL.txt).

The fonts are bundled rather than loaded from a CDN because the tool is
designed to run on an isolated hospital network with no outbound internet
access. They are redistributed unmodified; only the format was converted to
WOFF2 for web delivery.

Freesentation is itself derived from Source Han Sans (Noto Sans CJK) and Heebo,
both under the same licence.

The licence of the fonts is independent of the licence of this software.
To drop the fonts instead, delete the `.woff2` files and the `@font-face`
rules at the top of `../style.css`; the stylesheet falls back to the system
UI stack.
