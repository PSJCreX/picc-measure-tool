"""계측 변수 8종 정의 — 단일 진실원.

모든 영상에 대해 **8개 변수를 전부** 계측한다. 업로드 시 그룹을 나누지 않는다.
여덟 항목 모두 **드래그 한 번 = 두 점**으로 잰다 — 재는 방식이 항목마다 다르면
소요시간을 서로 비교할 수 없다.

계측의 단위는 영상이 아니라 **(영상 × 변수) 문제 하나**다.
한 문제 = 한 영상에서 한 변수를 재는 것이고, 그 하나에 걸린 시간이 곧
논문의 속도 지표가 된다.

여기 정의된 key는 그대로
  * DB `Measurement.variable_key`
  * 내보내기 컬럼명
  * 학습용 어노테이션 JSON 파일명 접미사 (`<base>_<key>.json`)
로 쓰인다. 기존 `data/annotations/<key>/` 폴더 이름과 일치시켰다.

계수(coeff)는 `picc_length_prediction_using_formula.py:724`의 회귀공식과 동일하다.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Variable:
    key: str
    display: str          # 문제 카드 제목
    short: str            # 매트릭스/내보내기 열 머리글
    coeff: float          # 논문 공식 계수 (참고 표시용)
    instruction: str      # 문제 출제 화면에서 "무엇을 재는가" (한 줄 요약)
    guide: str            # static/graphic/ 안내 그림 파일명
    # 학습용 JSON 저장 키. 기존 `data/annotations/<key>/` 포맷을 그대로 따르려고
    # 남겨 둔 것으로, 계측 방식과는 무관하다 — **8개 항목 모두 두 점**이다.
    # (데스크톱 툴은 clavicle/two_vbu를 4~5점으로 찍었지만, 길이는 어차피
    #  첫 점↔끝 점 직선거리라 두 점으로 통일했다. 추론 코드의 계산과 동일하다.)
    ann_shape: str        # "points" | "line"

    # ── 아래는 문제 출제 화면(브리핑)에만 쓰인다 ──────────────────────────
    # 논문 "Parameter measurements from the post-procedural AP-CXR" 절의 정의를
    # 그대로 병기한다. 한글 안내만 두면 번역 과정에서 정의가 미묘하게 흔들리고,
    # 계측자마다 다르게 해석하면 그 편차가 그대로 spread_mm 으로 들어온다.
    paper_en: str         # 논문 원문 정의 (영문 병기)
    # 두 점은 **순서를 규정하지 않는다.** 길이는 첫↔끝 직선거리라 대칭이고
    # (`geometry.pixel_length`), 모델도 점 순서를 무시하도록 학습돼 있다.
    # 계측자가 편한 쪽부터 잡게 두는 편이 자연스럽고 빠르다. 그래서 화면에도
    # '시작점/끝점'이 아니라 Point A / Point B 로 띄운다.
    point_a: str          # 두 점 중 하나가 어디인가
    point_b: str          # 나머지 하나가 어디인가
    caution: str          # 상식적으로 놓치기 쉬운 지점 (화면에서 강조 표시)
    axis: str             # "horizontal" | "vertical" | "oblique" — 방향 배지

    # 안내문 안에서 [[...]] 로 감싼 부분은 화면에서 별도 색으로 강조된다.
    # 정의의 급소(중간점·내연·상연 종판 같은 것)만 감쌀 것 — 다 강조하면
    # 아무것도 강조되지 않는다.


# ── 축 기준 안내 ────────────────────────────────────────────────────────────
# 논문의 horizontal / vertical 은 **영상이 아니라 환자** 기준이다. 이 데이터는
# 응급실·중환자실 침상 이동형 촬영이라 환자가 기울거나 회전한 채 찍힌 영상이
# 흔한데, 그때 영상의 가로줄(같은 y)을 따라 재면 환자의 횡단면이 아닌 사선을
# 재게 된다.
#
# 장축을 잡는 랜드마크는 일부러 못박지 않는다. scoliosis 처럼 척추가 휘어 있으면
# '척추체 중심선' 같은 기계적 기준이 오히려 틀리고, 머리–골반 중심을 이어야
# 하나 싶을 만큼 주관적이 된다. 원칙만 주고 판단은 계측자에게 맡긴다.
#
# 항목마다 반복해 적지 않고 axis 로 골라 쓴다 — 같은 규칙이 항목마다 조금씩
# 다르게 적히면 그 차이가 그대로 계측자 간 해석 차이가 된다.
AXIS_NOTE: dict[str, str] = {
    "horizontal": (
        "[[영상의 가로 방향이 아닙니다.]] 이 영상에 찍힌 "
        "[[환자의 장축(머리–발 방향)에 직각]]으로 재세요. "
        "침상 촬영이라 환자가 기울거나 회전한 채 찍힌 영상이 많은데, "
        "그러면 계측선도 그 기울기만큼 함께 기울어야 합니다. "
        "장축을 어디로 잡을지는 [[계측자 판단]]에 맡깁니다 — 척추 변형이 있으면 "
        "기계적인 기준선이 오히려 틀립니다. 이 영상에서 환자가 어떻게 누워 "
        "있는지를 보고 정하세요."
    ),
    "vertical": (
        "[[영상의 세로 방향이 아닙니다.]] 이 영상에 찍힌 "
        "[[환자의 장축(머리–발 방향)]]을 따라 재세요. "
        "척추체를 잇는 계측이라 대개 해부학적으로 따라가지만, 환자가 기울어 "
        "있으면 계측선도 함께 기웁니다. 장축 판단은 [[계측자 재량]]입니다."
    ),
    # 사선 항목(쇄골)은 두 해부학적 끝점을 잇는 것이라 축 기준이 필요 없다.
    "oblique": "",
}


def axis_note(axis: str) -> str:
    return AXIS_NOTE.get(axis, "")


# 안내 그림은 old_calculator/graphic/ 에서 가져왔다. 파일명의 Rt_/Lt_ 는
# "우측/좌측 **접근**" 기준이라 해부학적 좌우와 반대인 경우가 있다 —
# 쇄골 그림이 그렇다. Rt_cCL.png 는 '우측 접근의 반대측 쇄골' = **좌측 쇄골**을
# 강조한 그림이므로 left_clavicle 에 붙인다.
VARIABLES: dict[str, Variable] = {
    "right_clavicle": Variable(
        key="right_clavicle",
        display="Rt. Clavicle",
        short="Rt.CL",
        coeff=-0.062,
        instruction="오른쪽 쇄골의 [[양 끝 각각의 중간점]] 사이 거리",
        guide="Lt_cCL.png",
        ann_shape="points",
        paper_en=(
            "Length between the midpoints of the clavicle's proximal and "
            "distal ends (contralateral clavicle length [cCL])."
        ),
        point_a="오른쪽 쇄골 흉골단(proximal, 몸통 쪽 끝) — 그 끝면의 [[상·하 경계 중간점]]",
        point_b="오른쪽 쇄골 견봉단(distal, 어깨 쪽 끝) — 그 끝면의 [[상·하 경계 중간점]]",
        caution=(
            "끝의 [[경계선(border) 위가 아니라 그 중간점]]을 찍습니다. "
            "쇄골 끝은 위·아래로 두께가 있으므로, 그 [[위·아래 경계의 가운데]]를 눈으로 잡아 "
            "거기에 점을 두세요. 위쪽 모서리나 아래쪽 모서리를 찍으면 길이가 달라집니다."
        ),
        axis="oblique",
    ),
    "left_clavicle": Variable(
        key="left_clavicle",
        display="Lt. Clavicle",
        short="Lt.CL",
        coeff=-0.062,
        instruction="왼쪽 쇄골의 [[양 끝 각각의 중간점]] 사이 거리",
        guide="Rt_cCL.png",
        ann_shape="points",
        paper_en=(
            "Length between the midpoints of the clavicle's proximal and "
            "distal ends (contralateral clavicle length [cCL])."
        ),
        point_a="왼쪽 쇄골 흉골단(proximal, 몸통 쪽 끝) — 그 끝면의 [[상·하 경계 중간점]]",
        point_b="왼쪽 쇄골 견봉단(distal, 어깨 쪽 끝) — 그 끝면의 [[상·하 경계 중간점]]",
        caution=(
            "끝의 [[경계선(border) 위가 아니라 그 중간점]]을 찍습니다. "
            "쇄골 끝은 위·아래로 두께가 있으므로, 그 [[위·아래 경계의 가운데]]를 눈으로 잡아 "
            "거기에 점을 두세요. 위쪽 모서리나 아래쪽 모서리를 찍으면 길이가 달라집니다."
        ),
        axis="oblique",
    ),
    "right_hvd": Variable(
        key="right_hvd",
        display="Rt. HVD",
        short="Rt.HVD",
        coeff=0.720,
        instruction="오른쪽 상완골두 내측연에서 척추 정중선까지의 [[최단 수평거리]]",
        guide="Rt_HVD.png",
        ann_shape="line",
        paper_en=(
            "Shortest horizontal distance between ipsilateral humerus head "
            "medial border and vertebra midline (humero-vertebral distance [HVD])."
        ),
        point_a="오른쪽 [[상완골두(humeral head)의 내측 경계]] — 정중선에 가장 가까운 지점",
        point_b="[[척추 정중선(vertebral midline)]] — 장축에 직각으로 만나는 지점",
        caution=(
            "[[최단]] 거리입니다 — 상완골두 내측연에서 척추 정중선까지 "
            "[[가장 가까워지는 지점]]을 잡으세요. 상완골두 경계는 둥글기 때문에 "
            "어디를 잡느냐에 따라 값이 달라집니다."
        ),
        axis="horizontal",
    ),
    "left_hvd": Variable(
        key="left_hvd",
        display="Lt. HVD",
        short="Lt.HVD",
        coeff=0.720,
        instruction="왼쪽 상완골두 내측연에서 척추 정중선까지의 [[최단 수평거리]]",
        guide="Lt_HVD.png",
        ann_shape="line",
        paper_en=(
            "Shortest horizontal distance between ipsilateral humerus head "
            "medial border and vertebra midline (humero-vertebral distance [HVD])."
        ),
        point_a="왼쪽 [[상완골두(humeral head)의 내측 경계]] — 정중선에 가장 가까운 지점",
        point_b="[[척추 정중선(vertebral midline)]] — 장축에 직각으로 만나는 지점",
        caution=(
            "[[최단]] 거리입니다 — 상완골두 내측연에서 척추 정중선까지 "
            "[[가장 가까워지는 지점]]을 잡으세요. 상완골두 경계는 둥글기 때문에 "
            "어디를 잡느냐에 따라 값이 달라집니다."
        ),
        axis="horizontal",
    ),
    "right_tcd": Variable(
        key="right_tcd",
        display="Rt. TCD",
        short="Rt.TCD",
        coeff=0.761,
        instruction="[[기관 융기 하연 높이]]에서, 오른쪽 흉강 외측연부터 기관분기점까지의 [[수평거리]]",
        guide="Rt_TCD.png",
        ann_shape="line",
        paper_en=(
            "Horizontal distance between the thoracic cavity lateral margin "
            "(parietal pleura, not visceral pleura; that is, an imaginary line "
            "connecting the inner edge of the rib) and carina bifurcation point "
            "at the level of the carina inferior border "
            "(thoraco-carinal distance [TCD])."
        ),
        point_a=(
            "오른쪽 흉강 외측연 — [[늑골의 안쪽 가장자리를 이은 가상의 선]] "
            "(= 벽측 흉막 parietal pleura)"
        ),
        point_b="[[기관 분기점(carina bifurcation point)]]",
        caution=(
            "외측 기준은 [[폐 경계선(장측 흉막, visceral pleura)이 아닙니다]]. "
            "무기폐·흉수로 폐가 안쪽으로 밀려 있어도 [[늑골 내연을 이은 선]]을 씁니다. "
            "높이는 [[기관 융기의 하연(inferior border)]]에 맞춥니다 — "
            "이 '높이'도 [[환자 장축에 직각]]인 선이지 영상의 가로줄이 아닙니다."
        ),
        axis="horizontal",
    ),
    "left_tcd": Variable(
        key="left_tcd",
        display="Lt. TCD",
        short="Lt.TCD",
        coeff=0.761,
        instruction="[[기관 융기 하연 높이]]에서, 왼쪽 흉강 외측연부터 기관분기점까지의 [[수평거리]]",
        guide="Lt_TCD.png",
        ann_shape="line",
        paper_en=(
            "Horizontal distance between the thoracic cavity lateral margin "
            "(parietal pleura, not visceral pleura; that is, an imaginary line "
            "connecting the inner edge of the rib) and carina bifurcation point "
            "at the level of the carina inferior border "
            "(thoraco-carinal distance [TCD])."
        ),
        point_a=(
            "왼쪽 흉강 외측연 — [[늑골의 안쪽 가장자리를 이은 가상의 선]] "
            "(= 벽측 흉막 parietal pleura)"
        ),
        point_b="[[기관 분기점(carina bifurcation point)]]",
        caution=(
            "외측 기준은 [[폐 경계선(장측 흉막, visceral pleura)이 아닙니다]]. "
            "무기폐·흉수로 폐가 안쪽으로 밀려 있어도 [[늑골 내연을 이은 선]]을 씁니다. "
            "높이는 [[기관 융기의 하연(inferior border)]]에 맞춥니다 — "
            "이 '높이'도 [[환자 장축에 직각]]인 선이지 영상의 가로줄이 아닙니다."
        ),
        axis="horizontal",
    ),
    "second_ribs_width": Variable(
        key="second_ribs_width",
        display="2nd Ribs Width",
        short="2RHD",
        coeff=0.255,
        instruction="좌·우 제2늑골 [[내연(안쪽 가장자리)]] 사이의 [[최대 수평거리]]",
        guide="Rt_2RHD.png",
        ann_shape="line",
        paper_en=(
            "Maximal horizontal distance between the inner edges of the second "
            "ribs (second ribs horizontal distance [2RHD])."
        ),
        point_a="오른쪽 제2늑골의 [[안쪽 가장자리(inner edge)]] — 가장 넓어지는 지점",
        point_b="왼쪽 제2늑골의 [[안쪽 가장자리(inner edge)]] — 가장 넓어지는 지점",
        caution=(
            "[[바깥 가장자리가 아니라 안쪽 가장자리]]입니다 — 늑골 음영의 흉강 쪽 경계입니다. "
            "좌우가 [[가장 멀어지는(maximal)]] 지점을 찾아 그 자리에서 재세요."
        ),
        axis="horizontal",
    ),
    "two_vbu": Variable(
        key="two_vbu",
        display="Two VBUs",
        short="2VBU",
        coeff=1.024,
        instruction="[[기관 융기 하연 아래]] 척추체 2개 단위(2 VBUs)의 길이 — [[추간판 2개 포함]]",
        # 구 계산기의 Rt_2VBU.png 는 화살표가 아래 척추체의 **하연**에서 끝나
        # 추간판을 하나만 포함한다(정의보다 한 디스크 짧다). 그 그림을 따라간
        # 계측자들이 실제로 같은 실수를 했다. 교정 모식도로 교체한다.
        guide="2VBU_schematic.svg",
        ann_shape="points",
        paper_en=(
            "Two vertebral body units (2VBUs) below the inferior carina border. "
            "One VBU was defined as the distance unit between the superior "
            "endplate of one vertebra to the superior endplate of the next, "
            "with the intervertebral disk included."
        ),
        point_a="기관 융기 하연 아래 첫 척추체(①)의 [[상연 종판(superior endplate)]]",
        point_b="거기서 두 칸 아래 — 즉 [[세 번째 척추체(③)의 상연 종판]]",
        caution=(
            "[[추간판이 두 번 들어갑니다.]] 두 점 사이를 "
            "[[척추체 → 추간판 → 척추체 → 추간판]] 순서로 지나갑니다 — "
            "척추체 2개와 [[추간판 2개]]입니다.\n"
            "가장 흔한 실수는 [[추간판을 하나만 포함]]하는 것입니다. "
            "두 번째 척추체의 [[하연에서 멈추면]] 마지막 추간판이 빠져 "
            "한 디스크만큼 짧아집니다. 반대쪽 점은 [[세 번째 척추체의 상연 종판]]입니다 "
            "(1 VBU = 한 척추체의 상연 종판 → 다음 척추체의 상연 종판)."
        ),
        axis="vertical",
    ),
}

# 출제 순서와 무관한 정규 순서. 표·내보내기 열 순서로 쓴다.
ALL_VARIABLE_KEYS: tuple[str, ...] = (
    "right_clavicle",
    "left_clavicle",
    "right_hvd",
    "left_hvd",
    "right_tcd",
    "left_tcd",
    "second_ribs_width",
    "two_vbu",
)

N_VARIABLES = len(ALL_VARIABLE_KEYS)


def variable(key: str) -> Variable | None:
    return VARIABLES.get(key)


def is_valid_variable(key: str) -> bool:
    return key in VARIABLES


def ordered_variables() -> list[Variable]:
    return [VARIABLES[k] for k in ALL_VARIABLE_KEYS]
