"""Prepare the real, public site screens for the user's screenshot review.

These adapters leave every browser page open.  A screen is ready only after its
visible result has been checked; an unresolved place or sign-in stays manual.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Any
from urllib.parse import urlparse

from .models import FUEL_LABELS, LogCallback, PreparedPage, RunRequest


NAVER_URL = "https://map.naver.com/p/directions/-/-/-/car"
OPINET_URL = "https://www.opinet.co.kr/user/dopospdrg/dopOsPdrgSelect.do"
HIPASS_URL = "https://www.hipass.co.kr/usepculr/InitUsePculrTabSearch.do"
HIPASS_LOGIN_URL = "https://www.hipass.co.kr/comm/lginpg.do?any_tmptab=01"
ACTION_TIMEOUT = 12_000
NAVIGATION_TIMEOUT = 25_000


def _new_page(context: Any) -> Any:
    page = context.new_page()
    page.set_default_timeout(ACTION_TIMEOUT)
    page.set_default_navigation_timeout(NAVIGATION_TIMEOUT)
    return page


def _details(request: RunRequest, requested_url: str) -> dict[str, str]:
    return {"trip_date": request.trip_date, "requested_url": requested_url}


def _manual(page: Any, message: str, details: dict[str, str], log: LogCallback) -> PreparedPage:
    details["url"] = page.url
    log(message)
    return PreparedPage(page=page, ready=False, message=message, details=details)


def _normalise(text: str) -> str:
    return re.sub(r"[\s,·ㆍ()\[\]]+", "", text).casefold()


def _visible(locator: Any) -> list[Any]:
    return [item for item in locator.all() if item.is_visible()]


def _check(locator: Any, checked: bool) -> None:
    if locator.is_checked() == checked:
        return
    if locator.is_visible():
        locator.set_checked(checked)
    else:
        element_id = locator.get_attribute("id")
        if not element_id:
            raise ValueError("체크 항목의 연결된 레이블을 찾을 수 없습니다.")
        locator.page.locator(f'label[for="{element_id}"]').click()
    if locator.is_checked() != checked:
        raise ValueError("체크 항목 적용을 확인할 수 없습니다.")


def _candidate_matches(candidate: Any, query: str) -> bool:
    """Require a concrete place row, rather than an autocomplete query string."""
    text = candidate.inner_text().strip()
    if not text:
        return False
    normal_query = _normalise(query)
    # Actual place rows show an address.  A bare autocomplete suggestion with
    # the same name is not enough evidence to select a location.
    address_pattern = r"(?:[가-힣]+(?:특별시|광역시|특별자치시|도|시|군|구|읍|면|동|리)\s|[가-힣0-9]+(?:로|길)\s*\d)"
    if not re.search(address_pattern, text):
        return False
    title_selectors = ".search_result_title, .place_text:not(.sub), .place_name, .title, .name, .text_name"
    titles = [item.inner_text().strip() for item in candidate.locator(title_selectors).all()]
    if any(_normalise(title) == normal_query for title in titles):
        return True
    # A fully specified address can uniquely identify a result even when its
    # displayed establishment name differs from the user's address input.
    is_address = bool(re.search(r"(?:로|길|동|리)\s*\d", query))
    return is_address and normal_query in _normalise(text)


def _choose_naver_place(page: Any, field: Any, query: str, label: str, log: LogCallback) -> str | None:
    field.fill(query)
    # Wait for the typed field's own autocomplete, with its visible title and
    # address, rather than applying Naver's highlighted first suggestion.
    input_container = field.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' reorderable_item ')][1]")
    instant_options = input_container.locator('.direction_search_result_list [role="option"]')
    try:
        instant_options.filter(has_text=query).first.wait_for(state="visible", timeout=10_000)
        instant_matches = [option for option in _visible(instant_options) if _candidate_matches(option, query)]
        if len(instant_matches) == 1:
            selected_text = instant_matches[0].inner_text().strip()
            instant_matches[0].click()
            point_index = 3 if label == "출발지" else 4
            page.wait_for_function("""index => {
                const point = location.pathname.split('/')[index];
                return point && point !== '-';
            }""", arg=point_index, timeout=12_000)
            log(f"네이버 지도 {label}의 일치하는 장소를 선택했습니다.")
            return selected_text
        if len(instant_matches) > 1:
            log(f"네이버 지도 {label}: 같은 이름의 장소가 여러 개입니다. 주소를 확인해 직접 선택해 주세요.")
            return None
    except Exception:
        log(f"네이버 지도 {label}: 검색 후보 자동 확인을 완료하지 못했습니다.")
    # Enter can silently apply Naver's highlighted first suggestion.  Leave an
    # unresolved query as typed, so only the user chooses an ambiguous place.
    log(f"네이버 지도 {label}: 장소를 하나로 확인하지 못했습니다. 브라우저에서 후보를 직접 선택해 주세요.")
    return None


def _naver_find_button(page: Any) -> Any | None:
    buttons = _visible(page.get_by_role("button", name="길찾기", exact=True))
    if len(buttons) == 1:
        return buttons[0]
    # Distinguish the route form submit button from the sidebar navigation.
    matches = []
    for button in buttons:
        in_route_form = button.evaluate("""element => {
            let parent = element.parentElement;
            for (let depth = 0; parent && depth < 7; depth++, parent = parent.parentElement) {
                if (parent.tagName === 'NAV') return false;
                if (parent.querySelectorAll('input[role="combobox"]').length >= 2) return true;
            }
            return false;
        }""")
        if in_route_form:
            matches.append(button)
    return matches[0] if len(matches) == 1 else None


def _set_naver_fuel(page: Any, request: RunRequest, details: dict[str, str], log: LogCallback) -> None:
    fuels = {"diesel": "경유", "gasoline": "휘발유", "premium": "고급휘발유"}
    if request.fuel not in fuels:
        details["route_fuel_status"] = "실내등유는 네이버 차량 연료 설정에서 지원하지 않음. 거리·경로 확인용이며 요금·연료비 유종 자동적용 안 됨."
        return
    target = fuels[request.fuel]
    settings = page.get_by_role("button", name=re.compile(r"차량 기준$"))
    settings.click()
    dialog = page.get_by_role("dialog").filter(has_text="차종/연료 설정")
    fuel = dialog.get_by_role("checkbox", name=target, exact=True)
    if not fuel.is_checked():
        field_id = fuel.get_attribute("id")
        dialog.locator(f'label[for="{field_id}"]').click()
    if not fuel.is_checked():
        raise ValueError("네이버 지도 연료 선택을 확인하지 못했습니다.")
    # Preserve the site's selected vehicle class, mileage and Hi-Pass setting.
    dialog.get_by_role("button", name="저장", exact=True).click()
    dialog.wait_for(state="hidden")
    expected = re.compile(rf"(?:^|\s){re.escape(target)}\s*차량 기준$")
    confirmed = page.get_by_role("button", name=expected)
    confirmed.wait_for(state="visible")
    details["route_fuel"] = target
    details["route_vehicle"] = confirmed.inner_text().strip()
    log(f"네이버 지도 연료 기준을 {target}로 설정했습니다.")


def prepare_naver(context: Any, request: RunRequest, log: LogCallback) -> PreparedPage:
    page = _new_page(context)
    details = _details(request, NAVER_URL)
    details.update(origin=request.origin, destination=request.destination,
                   route_time_basis="조회 시점의 교통상황이며 과거 출장일의 경로 재현이 아님")
    try:
        log("네이버 지도 자동차 길찾기 화면을 엽니다.")
        page.goto(NAVER_URL, wait_until="domcontentloaded")
        start = page.get_by_role("combobox", name="출발지 입력", exact=True)
        if start.count() == 0:
            page.get_by_role("button", name="길찾기", exact=True).click()
        page.get_by_role("tab", name="자동차", exact=True).click()
        start.wait_for(state="visible")
        goal = page.get_by_role("combobox", name="도착지 입력", exact=True)
        origin = _choose_naver_place(page, start, request.origin, "출발지", log)
        if origin is None:
            # Naver opens the destination autocomplete on focus, so populate
            # it only at handoff rather than overlaying the origin candidates.
            goal.fill(request.destination)
            start.click()
            return _manual(page, "출발지 후보를 직접 선택하고 도착지와 경로를 확인한 뒤 화면을 저장해 주세요.", details, log)
        details["selected_origin"] = origin
        destination = _choose_naver_place(page, goal, request.destination, "도착지", log)
        if destination is None:
            return _manual(page, "도착지 후보를 직접 선택하고 길찾기를 누른 뒤 화면을 저장해 주세요.", details, log)
        details["selected_destination"] = destination
        button = _naver_find_button(page)
        if button is None:
            return _manual(page, "브라우저에서 길찾기 버튼을 누르고 경로를 확인한 뒤 화면을 저장해 주세요.", details, log)
        button.click()
        page.wait_for_function("""() => {
            const text = document.body.innerText;
            return /\\d+(?:\\.\\d+)?\\s*km/i.test(text)
                && /\\d+\\s*(?:시간|분)/.test(text)
                && /통행료|연료비/.test(text);
        }""", timeout=25_000)
        path = urlparse(page.url).path.split("/")
        direction_index = path.index("directions")
        points = path[direction_index + 1:direction_index + 3]
        if len(points) != 2 or any(point in ("", "-") for point in points) or "car" not in path:
            return _manual(page, "자동차 경로 결과 주소를 확인하지 못했습니다. 출발지·도착지를 확인하고 직접 저장해 주세요.", details, log)
        if not start.input_value().strip() or not goal.input_value().strip():
            return _manual(page, "출발지·도착지 적용을 확인하지 못했습니다. 브라우저에서 확인한 뒤 저장해 주세요.", details, log)
        _set_naver_fuel(page, request, details, log)
        details["url"] = page.url
        message = "자동차 경로와 소요시간·거리를 확인했습니다. 화면을 검토한 뒤 저장해 주세요."
        if request.fuel == "kerosene":
            message = "자동차 경로와 거리를 확인했습니다. 네이버 지도는 등유 연료 설정을 지원하지 않으므로 요금·연료비를 별도로 확인해 주세요."
        return PreparedPage(page, True, message, details)
    except Exception as exc:
        return _manual(page, f"네이버 지도 자동 준비가 중단되었습니다({type(exc).__name__}). 브라우저에서 경로를 완성한 뒤 직접 저장해 주세요.", details, log)


def _has_opinet_result(page: Any, trip_day: date, fuel: str, check_controls: bool) -> bool:
    label = FUEL_LABELS[fuel]
    if check_controls:
        for prefix in ("STA", "END"):
            for suffix, expected in (("Y", trip_day.year), ("M", trip_day.month), ("D", trip_day.day)):
                if int(page.locator(f"#{prefix}_{suffix}").input_value()) != expected:
                    return False
        if not page.locator('input[name="TERM"][value="D"]').is_checked():
            return False
    tables = _visible(page.locator("table"))
    date_pattern = re.compile(rf"{trip_day.year}\s*년\s*0?{trip_day.month}\s*월\s*0?{trip_day.day}\s*일")
    for table in tables:
        headings = _normalise(" ".join(table.locator("th").all_inner_texts()))
        if _normalise(label) not in headings:
            continue
        if any(other != fuel and _normalise(other_label) in headings for other, other_label in FUEL_LABELS.items()):
            continue
        for row in table.locator("tr").all():
            text = row.inner_text()
            if date_pattern.search(text):
                values = [cell.inner_text().strip() for cell in row.locator("td").all()]
                if any(re.fullmatch(r"\d[\d,]*\.\d+", value) for value in values):
                    return True
    return False


def prepare_opinet(context: Any, request: RunRequest, log: LogCallback) -> PreparedPage:
    page = _new_page(context)
    details = _details(request, OPINET_URL)
    details["fuel"] = FUEL_LABELS[request.fuel]
    popup = None
    try:
        log("오피넷 제품별 평균판매가격을 조회합니다.")
        page.goto(OPINET_URL, wait_until="domcontentloaded")
        trip_day = date.fromisoformat(request.trip_date)
        page.locator("#STA_Y").wait_for(state="visible")
        maximum = page.locator("#h_maxDD").input_value()
        if maximum and trip_day.strftime("%Y%m%d") > maximum:
            return _manual(page, "해당 출장일의 일일 유가가 아직 공표되지 않았습니다. 공개 날짜를 확인한 뒤 직접 저장해 주세요.", details, log)
        _check(page.locator('input[name="TERM"][value="D"]'), True)
        for prefix in ("STA", "END"):
            page.locator(f"#{prefix}_Y").select_option(str(trip_day.year))
            page.locator(f"#{prefix}_M").select_option(f"{trip_day.month:02d}")
            page.locator(f"#{prefix}_D").select_option(f"{trip_day.day:02d}")
        products = {"premium": "chk2_1", "gasoline": "chk2_2", "diesel": "chk2_3", "kerosene": "chk2_5"}
        # Check the desired product first so the form never has zero products.
        _check(page.locator(f"#{products[request.fuel]}"), True)
        for fuel, element_id in products.items():
            if fuel != request.fuel:
                _check(page.locator(f"#{element_id}"), False)
        obsolete = page.locator("#chk2_6")
        if obsolete.count():
            _check(obsolete, False)
        page.locator("#btn_search").click()
        # The link first changes its hash, then submits the form. Waiting on
        # navigation alone can return while yesterday's result is still shown.
        page.wait_for_function("""({day, fuel}) => Array.from(document.querySelectorAll('table')).some(table => {
            const headings = Array.from(table.querySelectorAll('thead th')).map(node => node.innerText.trim());
            const rows = Array.from(table.querySelectorAll('tbody tr'));
            return headings.length === 2 && headings[1].replace(/\\s/g, '') === fuel
                && rows.some(row => row.innerText.replace(/\\s/g, '').includes(day));
        })""", arg={"day": trip_day.strftime("%Y년%m월%d일"), "fuel": FUEL_LABELS[request.fuel]}, timeout=25_000)
        if not _has_opinet_result(page, trip_day, request.fuel, True):
            return _manual(page, "조회 결과의 출장일·유종·가격을 확인하지 못했습니다. 오피넷에서 조건을 확인한 뒤 화면인쇄로 저장해 주세요.", details, log)
        details["verified_date"] = request.trip_date
        details["price_scope"] = "전국 주유소 제품별 일일 평균판매가격(부가세 포함, 원/리터)"
        with page.expect_popup(timeout=20_000) as opened:
            page.locator("#btn_Print").click()
        popup = opened.value
        popup.set_default_timeout(ACTION_TIMEOUT)
        popup.wait_for_url("**/dopOsPdrgPrint.do", timeout=20_000)
        popup.wait_for_load_state("domcontentloaded", timeout=20_000)
        if not _has_opinet_result(popup, trip_day, request.fuel, False):
            return _manual(page, "인쇄 화면의 날짜·유종을 확인하지 못했습니다. 열린 인쇄창을 확인하거나 원래 화면에서 다시 조회해 주세요.", details, log)
        details["url"] = popup.url
        details["source_url"] = page.url
        popup.bring_to_front()
        return PreparedPage(popup, True, "출장일과 유종이 적용된 오피넷 인쇄 화면입니다. 검토한 뒤 저장해 주세요.", details)
    except Exception as exc:
        return _manual(page, f"오피넷 자동 준비가 중단되었습니다({type(exc).__name__}). 조회 조건을 확인하고 화면인쇄를 연 뒤 직접 저장해 주세요.", details, log)


def _plain_input(locator: Any) -> bool:
    if locator.count() != 1 or not locator.is_visible() or not locator.is_enabled() or not locator.is_editable():
        return False
    # Never remove readonly/disabled, inject a password, or bypass a vendor's
    # encrypted keyboard.  Such fields are left to the normal site login UI.
    attributes = locator.evaluate("element => Object.fromEntries(Array.from(element.attributes, attr => [attr.name, attr.value]))")
    if any(key.lower().startswith(("npk", "e2e", "data-keypad")) for key in attributes):
        return False
    return not re.search(r"(?:^|\s)(?:e2e_form|nppfs\S*)(?:\s|$)", attributes.get("class", ""), re.I)


def prepare_hipass(context: Any, request: RunRequest, log: LogCallback, password: str | None = None) -> PreparedPage:
    page = _new_page(context)
    details = _details(request, HIPASS_URL)
    # The worker owns the shared dialog handler (dismiss alerts and cancel
    # installation confirms).  Registering another one here would dismiss the
    # same dialog twice.  Security fields remain untouched below.
    try:
        log("하이패스 사용내역 화면을 엽니다.")
        page.goto(HIPASS_URL, wait_until="domcontentloaded")
        if "로그인" in page.title() or "login" in page.url.casefold() or "lgin" in page.url.casefold():
            if request.hipass_id and password:
                page.goto(HIPASS_LOGIN_URL, wait_until="domcontentloaded")
                user_id = page.locator("#per_user_id")
                user_password = page.locator("#per_passwd")
                if _plain_input(user_id) and _plain_input(user_password):
                    user_id.fill(request.hipass_id)
                    user_password.fill(password)
                    page.locator("#per_login").click()
                    try:
                        page.get_by_text("로그아웃", exact=True).first.wait_for(state="visible", timeout=12_000)
                        page.goto(HIPASS_URL, wait_until="domcontentloaded")
                    except Exception:
                        return _manual(page, "하이패스 로그인에 추가 인증 또는 직접 확인이 필요합니다. 브라우저에서 로그인한 뒤 사용내역·출장일·영수증을 선택해 주세요.", details, log)
                else:
                    return _manual(page, "하이패스 보안 입력 또는 보안프로그램 확인이 필요합니다. 사이트의 정상 로그인 절차를 직접 완료한 뒤 사용내역·출장일·영수증을 선택해 주세요.", details, log)
            else:
                return _manual(page, "브라우저에서 하이패스에 로그인한 뒤 사용내역을 열고 출장일과 카드·영수증을 선택해 주세요. 선택한 영수증 화면을 직접 저장할 수 있습니다.", details, log)
        return _manual(page, "하이패스 사용내역에서 출장일과 카드를 확인하고 해당 영수증을 선택한 뒤 화면을 저장해 주세요.", details, log)
    except Exception as exc:
        # Do not expose exception call logs: fill() errors may include a secret.
        return _manual(page, f"하이패스 화면 준비가 중단되었습니다({type(exc).__name__}). 브라우저에서 로그인·조회·영수증 선택을 완료한 뒤 직접 저장해 주세요.", details, log)
