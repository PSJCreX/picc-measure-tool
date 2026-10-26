# 공개 절차

이 폴더는 `AI-PICC project/web_labeling` 에서 떠낸 **공개용 복사본**이다.
원본은 그대로 운영 중이고, 여기서 무엇을 고쳐도 운영에 영향이 없다.

푸시 전마다: `bash scripts/check_release.sh` — **FAIL 이 0 이어야 한다.**

---

## 정리된 것

- 안내 그림 — 저자가 계산기용으로 직접 그린 것이라 외부 저작권 문제 없음
- 라이선스 — MIT, 저작권자 Park Sung-Joon
- 폰트 — `static/fonts/OFL.txt` 에 공식 배포본의 OFL-1.1 전문을 넣었다
  (Freesentation, 공식 저장소 `github.com/Freesentation/freesentation` 에서 받음)

## 남은 것

- `CITATION.cff` · `.zenodo.json` 의 **소속(`<AFFILIATION>`)**
- `CITATION.cff` 의 **ORCID** (주석으로 비워 둠)
- 타깃 저널의 코드 공개 정책 — 선호 리포지터리, 공개 시점
- 모델 가중치 공개 여부는 **별도 판단** (이 저장소에 없음)

---

## 1. GitHub 공개 저장소

```bash
cd /Users/crex/LOCAL-DEV/picc-measure-tool
bash scripts/check_release.sh          # FAIL 0 확인

gh repo create PSJCreX/picc-measure-tool --public \
  --description "Web-based expert measurement tool with per-item timing, for an AI-versus-radiologist comparison on chest radiographs" \
  --source . --remote origin --push
```

`gh` 가 없으면 웹에서 빈 저장소를 만들고:

```bash
git remote add origin https://github.com/PSJCreX/picc-measure-tool.git
git push -u origin main
```

> **Zenodo 연동은 공개 저장소만 된다.** 심사 중 비공개로 두려면 §5 를 본다.

## 2. Zenodo 연동 (1회)

1. <https://zenodo.org> 로그인 — ORCID 계정으로 로그인하면 DOI 에 ORCID 가 붙는다.
2. 우상단 메뉴 → **GitHub**
3. GitHub 계정 **Connect** → 권한 승인
4. 저장소 목록에서 `picc-measure-tool` 의 스위치를 **ON**
   (안 보이면 **Sync now**)

이 시점에는 아직 DOI 가 없다. Zenodo 는 **릴리스가 생길 때만** DOI 를 만든다 —
커밋이나 태그만으로는 아무 일도 일어나지 않는다.

## 3. 릴리스 = DOI

```bash
git tag -a v1.0.0 -m "Reader-study measurement tool as used in the manuscript"
git push origin v1.0.0

gh release create v1.0.0 \
  --title "v1.0.0 — manuscript version" \
  --notes "The version of the tool used to collect the reader measurements reported in the manuscript."
```

웹 UI 로 해도 된다 (Releases → Draft a new release). 릴리스를 만들면 Zenodo 가
webhook 을 받아 스냅샷을 보관하고 **DOI 두 개**를 만든다.

| DOI | 가리키는 것 | 논문에서 |
|---|---|---|
| **version DOI** | 이 릴리스 하나 (v1.0.0) | **이걸 인용한다** — 쓴 코드가 고정된다 |
| concept DOI | 저장소 전체(모든 버전) | 최신판으로 보내고 싶을 때 |

Zenodo 레코드가 뜨는 데 몇 분 걸릴 수 있다. **DOI 를 논문에 넣기 전에 레코드를
열어 파일이 실제로 들어갔는지 확인한다.**

`.zenodo.json` 이 메타데이터를 미리 채우므로 Zenodo 화면에서 손댈 것은 거의 없다.
소속·ORCID 는 거기서도 고칠 수 있다.

## 4. 논문 문안

```
Code availability
The web-based measurement tool used for the reader study is openly
available at https://github.com/PSJCreX/picc-measure-tool and archived
at Zenodo (doi:10.5281/zenodo.XXXXXXX). The trained model weights and
inference code are available from the corresponding author upon
reasonable request, subject to institutional approval.

Data availability
The chest radiographs analysed in this study are not publicly available
because they contain protected health information.
```

`XXXXXXX` 는 **version DOI** 번호다.

## 5. 심사 중에는 비공개로 두고 싶다면

GitHub 비공개 저장소는 Zenodo 가 보관하지 못한다. 두 갈래다.

- **Zenodo 직접 업로드** — 저장소 압축본을 Zenodo 에 직접 올리고
  access 를 *Restricted* 로 두면 DOI 는 즉시 발급되고, **secret link** 를
  심사자에게 줄 수 있다. 게재 시 *Open* 으로 바꾼다.
- **게재 시 공개** — 본문에 "will be made publicly available upon
  publication" 으로 쓴다. 다만 이 문구는 "available on request" 만큼은
  아니어도 심사자에게 덜 선호된다.

> 이 저장소에는 환자 정보도 모델 가중치도 없다. 지금 공개하고 version DOI 를
> 투고에 넣는 쪽이 간단하고, 애초에 "on request" 를 피하려던 목적에 맞는다.

## 6. 공개 후

- GitHub 저장소 설명에 DOI 배지를 붙인다 (Zenodo → GitHub 화면에서 제공)
- `CITATION.cff` 가 main 에 있으면 GitHub 가 **Cite this repository** 버튼을 띄운다
- 코드를 고쳐 다시 릴리스하면 version DOI 가 새로 발급된다. **논문에 인용한
  DOI 는 그대로 그 릴리스를 가리킨다** — 그게 version DOI 를 쓰는 이유다
