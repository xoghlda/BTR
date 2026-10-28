"""Open Korail's observed receipt entry and verify individual receipt screens.

No reservation, payment, cancellation or authentication action is performed.
The entry page is a manual step: a purchase-history table is not a receipt.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from .models import LogCallback, PreparedPage, RunRequest


KORAIL_URL = "https://www.korail.com/ticket"
# Observed in the public menu's login redirect, not an inferred private API.
KORAIL_RECEIPT_URL = "https://www.korail.com/ticket/mypage/ticketInfo/receipt"
ACTION_TIMEOUT = 10_000
NAVIGATION_TIMEOUT = 25_000
_HOSTS = ("korail.com", "letskorail.com")
_RECEIPT_HEADING = re.compile(r"^(?:코레일\s*)?(?:승차권\s*)?(?:운임\s*)?영수증(?:\s*\([^)]*\))?$")
_HISTORY_HEADING = re.compile(r"(?:이용\s*(?:내역|실적).*(?:영수증|조회)|승차권\s*구입\s*이력)")
_DATE_LABELS = ("열차출발일", "승차일자", "승차일시", "출발일시", "승차일", "이용일", "여행일", "출발일")
_ORIGIN_LABELS = ("출발역", "승차역")
_DESTINATION_LABELS = ("도착역", "하차역")


class KorailReceiptError(ValueError):
    """The visible screen cannot yet be verified as the requested receipt."""


def _official_url(url: str) -> bool:
    if not isinstance(url, str):
        return False
    if url.startswith("blob:"):
        url = url[5:]
    host = (urlsplit(url).hostname or "").lower()
    return any(host == domain or host.endswith("." + domain) for domain in _HOSTS)


def _official_owner(page: Any) -> bool:
    visited: set[int] = set()
    for _ in range(10):
        if page is None or id(page) in visited:
            return False
        visited.add(id(page))
        try:
            if _official_url(page.url):
                return True
            page = page.opener()
        except Exception:
            return False
    return False


def _visible(locator: Any) -> list[Any]:
    return [item for item in locator.all() if item.is_visible()]


def _has_password_form(page: Any) -> bool:
    frames = getattr(page, "frames", None) or [page]
    for frame in frames:
        if _visible(frame.locator('input[type="password"]')):
            return True
    return False


def _headings(scope: Any) -> list[str]:
    return [item.inner_text().strip() for item in _visible(scope.locator('h1, h2, h3, [role="heading"], legend'))]


def _receipt_scope(page: Any) -> tuple[Any, str]:
    dialogs = []
    for dialog in _visible(page.get_by_role("dialog")):
        if any(_RECEIPT_HEADING.fullmatch(value) for value in _headings(dialog)):
            dialogs.append(dialog)
    if len(dialogs) > 1:
        raise KorailReceiptError("영수증 창이 여러 개입니다. 저장할 영수증 하나만 열어 주세요.")
    if dialogs:
        return dialogs[0], "영수증 대화상자"
    headings = _headings(page)
    if any(_HISTORY_HEADING.search(value) for value in headings):
        raise KorailReceiptError("이용내역 목록은 영수증으로 저장하지 않습니다. 해당 승차권의 운임영수증을 열어 주세요.")
    title = page.title().strip()
    if not any(_RECEIPT_HEADING.fullmatch(value) for value in headings + [title]):
        raise KorailReceiptError("공식 영수증 제목을 확인하지 못했습니다. 운임영수증을 열거나 PDF로 저장해 가져와 주세요.")
    return page.locator("body"), "개별 영수증 화면"


def _labelled_values(text: str, labels: tuple[str, ...]) -> list[str]:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    labels_pattern = "|".join(re.escape(label) for label in sorted(labels, key=len, reverse=True))
    pattern = re.compile(rf"^(?:{labels_pattern})\s*[:：]?\s*(.*)$")
    values = []
    for index, line in enumerate(lines):
        matched = pattern.match(line)
        if not matched:
            continue
        value = matched.group(1).strip()
        if not value and index + 1 < len(lines):
            value = lines[index + 1]
        if value:
            values.append(value)
    return values


def _dates(values: list[str]) -> set[date]:
    result = set()
    pattern = re.compile(r"(?<!\d)(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})(?:\s*일)?(?!\d)")
    compact = re.compile(r"(?<!\d)(20\d{2})(\d{2})(\d{2})(?!\d)")
    for value in values:
        for match in list(pattern.finditer(value)) + list(compact.finditer(value)):
            try:
                result.add(date(*(int(part) for part in match.groups())))
            except ValueError:
                continue
    return result


def _station(value: str) -> str:
    value = re.sub(r"\(?\d{1,2}:\d{2}\)?", "", value).strip()
    value = re.sub(r"[\s()（）]+", "", value)
    return value[:-1] if value.endswith("역") else value


def _stations(text: str) -> tuple[str, str]:
    origins = {_station(value) for value in _labelled_values(text, _ORIGIN_LABELS)}
    destinations = {_station(value) for value in _labelled_values(text, _DESTINATION_LABELS)}
    if len(origins) == len(destinations) == 1:
        return origins.pop(), destinations.pop()
    pairs = []
    for value in _labelled_values(text, ("승차구간", "승차 구간", "이용구간", "이용 구간")):
        values = re.split(r"\s*(?:→|->|↔|~|－|—|–|\s-\s)\s*", value, maxsplit=1)
        if len(values) == 2:
            pairs.append(tuple(_station(value) for value in values))
    unique = set(pairs)
    if len(unique) == 1:
        return unique.pop()
    raise KorailReceiptError("영수증의 출발역과 도착역을 하나로 확인하지 못했습니다. 구간이 표시된 영수증 하나를 열어 주세요.")


def _requested_period(request: RunRequest) -> tuple[date, date]:
    try:
        start = date.fromisoformat(request.trip_date)
        end = date.fromisoformat(getattr(request, "trip_end_date", "") or request.trip_date)
    except (TypeError, ValueError) as error:
        raise KorailReceiptError("출장 시작일과 종료일을 올바르게 입력해 주세요.") from error
    if end < start:
        raise KorailReceiptError("출장 종료일은 시작일보다 빠를 수 없습니다.")
    return start, end


def korail_receipt_target(page: Any, *, trusted_popup: bool = False) -> Any:
    """Return the same visible receipt scope inspected by the verifier.

    Call inspect_korail_receipt first for request/date/route verification.  A
    scrollable dialog is rejected rather than silently saving a clipped receipt.
    """
    if (not trusted_popup and not _official_owner(page)) or _has_password_form(page):
        raise KorailReceiptError("로그인·인증 화면 대신 코레일 개별 영수증을 열어 주세요.")
    scope, kind = _receipt_scope(page)
    if kind == "영수증 대화상자":
        clipped = scope.evaluate("""element => [element, ...element.querySelectorAll('*')].some(node => {
            if (node.clientWidth <= 0 || node.clientHeight <= 0) return false;
            const style = getComputedStyle(node);
            return (/auto|scroll/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 2)
                || (/auto|scroll/.test(style.overflowX) && node.scrollWidth > node.clientWidth + 2);
        })""")
        if clipped:
            raise KorailReceiptError("영수증 일부가 스크롤 영역에 가려져 있습니다. 전체 영수증을 PDF로 저장한 뒤 가져와 주세요.")
    return scope


def inspect_korail_receipt(page: Any, request: RunRequest, *, trusted_popup: bool = False) -> dict[str, str]:
    """Return non-sensitive evidence fields, or raise KorailReceiptError.

    The verifier requires an individual receipt heading, Korail issuer,
    payment evidence, a labelled travel date, and a matching station pair.
    The requested outbound and return pairs are checked separately.  An empty
    return station defaults to the corresponding reverse outbound station.
    It never returns card numbers, passenger names or raw page contents.
    Only the worker may supply trusted_popup after checking its tracked Korail
    output-page identity.  This replaces origin provenance, not receipt checks.
    """
    try:
        if page.is_closed() or (not trusted_popup and not _official_owner(page)):
            raise KorailReceiptError("코레일에서 연결된 영수증 창을 선택해 주세요.")
        if _has_password_form(page) or re.search(r"/(?:login|signIn)(?:/|\?|$)", page.url, re.I):
            raise KorailReceiptError("로그인·인증 화면은 저장하지 않습니다. 로그인 후 개별 운임영수증을 열어 주세요.")
        scope, screen = _receipt_scope(page)
        text = scope.inner_text(timeout=ACTION_TIMEOUT)
        if not re.search(r"한국철도공사|코레일|\bKORAIL\b", text, re.I):
            raise KorailReceiptError("영수증의 한국철도공사 발행 표시를 확인하지 못했습니다.")
        if re.search(r"(?:취소|환불|반환)\s*영수증|(?:취소|환불|반환)\s*완료", text):
            raise KorailReceiptError("반환·환불 내역입니다. 실제 이용한 승차권의 운임영수증을 선택해 주세요.")
        payment_method = re.search(r"결제\s*(?:수단|방법)|지불\s*방법", text)
        reference = re.search(r"승인\s*번호|거래\s*번호|승차권\s*번호|카드\s*번호", text)
        amount = re.search(r"(?:결제\s*금액|영수\s*(?:금액|액)|운임\s*[·/]?\s*요금)\s*[:：]?\s*([\d,]+)\s*원", text)
        if not payment_method or not reference or not amount or int(amount.group(1).replace(",", "")) <= 0:
            raise KorailReceiptError("영수증의 결제수단·결제금액·거래 식별 표시를 확인하지 못했습니다. 개별 영수증을 열거나 PDF로 저장해 가져와 주세요.")
        travel_dates = _dates(_labelled_values(text, _DATE_LABELS))
        if len(travel_dates) != 1:
            raise KorailReceiptError("영수증의 승차일을 하나로 확인하지 못했습니다. 결제일이 아닌 승차일이 표시된 영수증을 선택해 주세요.")
        travel_date = travel_dates.pop()
        start, end = _requested_period(request)
        if not start <= travel_date <= end:
            raise KorailReceiptError("영수증 승차일이 입력한 출장 기간과 다릅니다. 기간에 맞는 영수증을 선택해 주세요.")
        requested_origin = _station(getattr(request, "rail_origin", "") or "")
        requested_destination = _station(getattr(request, "rail_destination", "") or "")
        if not requested_origin or not requested_destination or requested_origin == requested_destination:
            raise KorailReceiptError("철도 출발역과 도착역을 구분해 입력한 뒤 영수증을 확인해 주세요.")
        return_origin = _station(getattr(request, "rail_return_origin", "") or requested_destination)
        return_destination = _station(getattr(request, "rail_return_destination", "") or requested_origin)
        origin, destination = _stations(text)
        if (origin, destination) == (requested_origin, requested_destination):
            direction = "가는편"
        elif return_origin and return_destination and return_origin != return_destination and (origin, destination) == (return_origin, return_destination):
            direction = "오는편"
        else:
            raise KorailReceiptError("영수증 승차구간이 입력한 철도 출발역·도착역과 다릅니다.")
        return {"verified_date": travel_date.isoformat(), "rail_origin": origin,
                "rail_destination": destination, "rail_direction": direction,
                "승차일": travel_date.isoformat(), "출발역": origin, "도착역": destination,
                "verified_segment": f"{origin} → {destination}",
                "rail_fare": amount.group(1).replace(",", "") + "원",
                "receipt_verification": "영수증 제목·발행기관·결제정보·승차일·구간 확인", "receipt_screen": screen}
    except KorailReceiptError:
        raise
    except Exception as error:
        raise KorailReceiptError("영수증 화면 내용을 확인하지 못했습니다. 개별 영수증을 다시 열거나 PDF로 저장해 가져와 주세요.") from error


def _labelled_input(page: Any, names: tuple[str, ...]) -> Any | None:
    matches = []
    for name in names:
        for field in _visible(page.get_by_label(name, exact=True)):
            if field.is_enabled() and field.is_editable():
                matches.append(field)
    return matches[0] if len(matches) == 1 else None


def _fill_date(field: Any, value: date) -> bool:
    field_type = (field.get_attribute("type") or "text").lower()
    if field_type == "date":
        field.fill(value.isoformat())
        return True
    if field_type != "text":
        return False
    hint = (field.get_attribute("placeholder") or "") + " " + field.input_value()
    if re.search(r"yyyy\s*-\s*mm\s*-\s*dd|\d{4}-\d{2}-\d{2}", hint, re.I):
        field.fill(value.isoformat())
    elif re.search(r"yyyy\s*\.\s*mm\s*\.\s*dd|\d{4}\.\d{2}\.\d{2}", hint, re.I):
        field.fill(value.strftime("%Y.%m.%d"))
    elif re.search(r"yyyymmdd|(?<!\d)\d{8}(?!\d)", hint, re.I):
        field.fill(value.strftime("%Y%m%d"))
    else:
        return False
    return True


def _fill_history_filters(page: Any, request: RunRequest) -> str:
    # Never fill the station/date controls on the reservation search screen.
    if "/ticket/mypage/ticketInfo/receipt" not in page.url:
        return "로그인 후 이용내역/영수증 조회 화면에서 기간과 역을 확인해 주세요."
    if not any(_HISTORY_HEADING.search(value) for value in _headings(page)):
        return "조회 화면 구성을 확인한 뒤 기간과 역을 직접 설정해 주세요."
    start, end = _requested_period(request)
    filled = []
    date_fields = (
        (("조회 시작일", "조회기간 시작일", "승차 시작일", "시작일"), start, "시작일"),
        (("조회 종료일", "조회기간 종료일", "승차 종료일", "종료일"), end, "종료일"),
    )
    for labels, value, label in date_fields:
        field = _labelled_input(page, labels)
        if field is not None and _fill_date(field, value):
            filled.append(label)
    for labels, value, label in (
        (_ORIGIN_LABELS, getattr(request, "rail_origin", ""), "출발역"),
        (_DESTINATION_LABELS, getattr(request, "rail_destination", ""), "도착역"),
    ):
        field = _labelled_input(page, labels)
        if value and field is not None and (field.get_attribute("type") or "text") in ("text", "search"):
            field.fill(value)
            filled.append(label)
    if filled:
        return "·".join(filled) + "을 입력했습니다. 조회 조건과 역 선택을 직접 확인해 주세요."
    return "조회 기간과 철도 출발역·도착역을 직접 확인해 주세요."


def prepare_korail(context: Any, request: RunRequest, log: LogCallback) -> PreparedPage:
    """Open the observed receipt menu; sign-in and receipt choice stay manual."""
    details = {"trip_date": request.trip_date,
               "trip_end_date": getattr(request, "trip_end_date", "") or request.trip_date,
               "rail_origin": getattr(request, "rail_origin", "") or "",
               "rail_destination": getattr(request, "rail_destination", "") or "",
               "rail_return_origin": getattr(request, "rail_return_origin", "") or "",
               "rail_return_destination": getattr(request, "rail_return_destination", "") or "",
               "requested_url": KORAIL_RECEIPT_URL,
               "receipt_verification": "개별 영수증 선택·확인 필요"}
    existing = []
    verified = []
    for candidate in reversed(getattr(context, "pages", ())):
        if candidate.is_closed():
            continue
        try:
            metadata = inspect_korail_receipt(candidate, request)
            verified.append((candidate, metadata))
        except KorailReceiptError:
            pass
        if _official_url(candidate.url) and re.search(r"/(?:mypage/ticketInfo/receipt|login|signIn)(?:/|\?|$)", candidate.url, re.I):
            existing.append(candidate)
    if len(verified) == 1:
        page, metadata = verified[0]
        details.update(metadata)
        details["url"] = page.url
        message = "출장 기간과 철도 구간이 일치하는 개별 코레일 영수증을 확인했습니다."
        log(message)
        return PreparedPage(page=page, ready=True, message=message, details=details)
    page = existing[0] if existing else context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT)
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    try:
        log("코레일 이용내역/영수증 조회 메뉴를 엽니다.")
        page.goto(KORAIL_RECEIPT_URL, wait_until="domcontentloaded")
        page.wait_for_function("() => document.body && document.body.innerText.trim().length > 30", timeout=15_000)
        body = page.locator("body").inner_text(timeout=ACTION_TIMEOUT)
        if "서비스 이용 안내" in body and ("네이버 클라우드" in body or "권장 브라우저" in body):
            message = "코레일 접속 안내 화면이 표시됐습니다. PC 네트워크에서 코레일을 정상적으로 연 뒤 다시 진행하거나 영수증 PDF를 가져와 주세요."
        elif _has_password_form(page) or "로그인" in page.title() or re.search(r"/(?:login|signIn)(?:/|\?|$)", page.url, re.I):
            message = "브라우저에서 코레일에 직접 로그인·인증한 뒤 '로그인 후 이용내역 조회'를 누르세요. 이용내역에서 출장 기간·철도 구간을 확인하고 개별 운임영수증을 열어 주세요."
        else:
            details["rail_filter_status"] = _fill_history_filters(page, request)
            message = details["rail_filter_status"] + " 이용내역 목록 대신 해당 승차권의 개별 운임영수증을 열어 저장하세요. 별도 창이 보이지 않으면 영수증 PDF 가져오기나 일반 브라우저 영수증 저장을 이용하세요."
    except Exception as error:
        message = f"코레일 화면 자동 준비가 중단되었습니다({type(error).__name__}). 공식 홈페이지에서 직접 로그인 후 이용내역/영수증 조회를 열거나 영수증 PDF를 가져와 주세요."
    details["url"] = page.url
    log(message)
    return PreparedPage(page=page, ready=False, message=message, details=details)
