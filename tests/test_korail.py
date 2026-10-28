from __future__ import annotations

import unittest
from types import SimpleNamespace

from trip_capture.korail import KORAIL_RECEIPT_URL, KorailReceiptError, inspect_korail_receipt, korail_receipt_target, prepare_korail


class Elements:
    def __init__(self, *items):
        self.items = list(items)

    def all(self):
        return self.items


class Text:
    def __init__(self, text, visible=True):
        self.text, self.visible = text, visible

    def inner_text(self, **_kwargs):
        return self.text

    def is_visible(self):
        return self.visible


class Field(Text):
    def __init__(self, value="", *, field_type="text", placeholder="", editable=True):
        super().__init__(value)
        self.field_type, self.placeholder, self.editable = field_type, placeholder, editable
        self.fills = []

    def is_enabled(self):
        return self.editable

    def is_editable(self):
        return self.editable

    def get_attribute(self, name):
        return {"type": self.field_type, "placeholder": self.placeholder}.get(name)

    def input_value(self):
        return self.text

    def fill(self, value):
        self.fills.append(value)
        self.text = value


class Scope(Text):
    def __init__(self, text, headings=()):
        super().__init__(text)
        self.headings = list(headings)

    def locator(self, selector):
        if selector == 'h1, h2, h3, [role="heading"], legend':
            return Elements(*(Text(value) for value in self.headings))
        raise AssertionError("Unexpected scope selector: " + selector)

    def evaluate(self, _script):
        return getattr(self, "scroll_clipped", False)


class Page(Scope):
    def __init__(self, text, headings=("운임영수증",), *, url=KORAIL_RECEIPT_URL,
                 title="코레일", passwords=(), dialogs=(), opener=None, labels=None):
        super().__init__(text, headings)
        self.url, self._title, self.passwords = url, title, list(passwords)
        self.dialogs, self._opener, self.labels = list(dialogs), opener, labels or {}
        self.frames = [self]
        self.visits = []

    def locator(self, selector):
        if selector == "body":
            return self
        if selector == 'input[type="password"]':
            return Elements(*self.passwords)
        return super().locator(selector)

    def get_by_role(self, role):
        if role == "dialog":
            return Elements(*self.dialogs)
        raise AssertionError("Unexpected role: " + role)

    def get_by_label(self, name, exact):
        assert exact
        return Elements(*self.labels.get(name, ()))

    def is_closed(self):
        return False

    def title(self):
        return self._title

    def opener(self):
        return self._opener

    def set_default_timeout(self, _value):
        pass

    def set_default_navigation_timeout(self, _value):
        pass

    def goto(self, url, **_kwargs):
        self.visits.append(url)

    def wait_for_function(self, *_args, **_kwargs):
        pass


def request(**changes):
    values = dict(trip_date="2026-10-19", trip_end_date="2026-10-21", rail_origin="서울역", rail_destination="오송역")
    values.update(changes)
    return SimpleNamespace(**values)


def receipt(*, travel_date="2026-10-20", origin="서울", destination="오송"):
    # Invented verification fixture, not a user's account or journey record.
    return f"""운임영수증
한국철도공사 KORAIL
승차일 {travel_date}
출발역 {origin}
도착역 {destination}
결제수단 신용카드
승차권번호 TEST-ONLY-0001
카드번호 1111-XXXX-XXXX-2222
승객이름 가상승객
결제금액 18,500원
발행일 2026-10-22
"""


class KorailReceiptTests(unittest.TestCase):
    def test_individual_receipt_matches_date_and_station_pair_without_exposing_identity(self):
        metadata = inspect_korail_receipt(Page(receipt()), request())
        self.assertEqual(metadata["verified_date"], "2026-10-20")
        self.assertEqual(metadata["verified_segment"], "서울 → 오송")
        self.assertEqual(metadata["rail_direction"], "가는편")
        self.assertEqual(metadata["rail_fare"], "18500원")
        self.assertEqual((metadata["승차일"], metadata["출발역"], metadata["도착역"]), ("2026-10-20", "서울", "오송"))
        self.assertNotIn("가상승객", str(metadata))
        self.assertNotIn("1111", str(metadata))
        self.assertNotIn("TEST-ONLY", str(metadata))

    def test_return_trip_is_verified_within_the_same_requested_period(self):
        metadata = inspect_korail_receipt(Page(receipt(travel_date="2026.10.21", origin="오송역", destination="서울역")), request())
        self.assertEqual(metadata["rail_direction"], "오는편")
        self.assertEqual(metadata["verified_date"], "2026-10-21")

    def test_outbound_and_return_can_use_separately_requested_stations(self):
        trip = request(rail_origin="오송역", rail_destination="서울역",
                       rail_return_origin="용산역", rail_return_destination="오송역")
        outbound = inspect_korail_receipt(Page(receipt(origin="오송", destination="서울")), trip)
        inbound = inspect_korail_receipt(Page(receipt(travel_date="2026-10-21", origin="용산", destination="오송")), trip)
        self.assertEqual((outbound["rail_direction"], outbound["verified_segment"]), ("가는편", "오송 → 서울"))
        self.assertEqual((inbound["rail_direction"], inbound["verified_segment"]), ("오는편", "용산 → 오송"))

    def test_neighboring_station_is_not_assumed_to_match_the_requested_return(self):
        implicit_reverse = request(rail_origin="오송", rail_destination="서울")
        with self.assertRaisesRegex(KorailReceiptError, "승차구간"):
            inspect_korail_receipt(Page(receipt(origin="용산", destination="오송")), implicit_reverse)
        explicit_return = request(rail_origin="오송", rail_destination="서울",
                                  rail_return_origin="용산", rail_return_destination="오송")
        with self.assertRaisesRegex(KorailReceiptError, "승차구간"):
            inspect_korail_receipt(Page(receipt(origin="청량리", destination="오송")), explicit_return)

    def test_history_table_with_receipt_words_is_never_treated_as_a_receipt(self):
        page = Page("이용내역/영수증 조회\n조회기간\n" + receipt(), headings=("이용내역/영수증 조회",))
        with self.assertRaisesRegex(KorailReceiptError, "이용내역 목록"):
            inspect_korail_receipt(page, request())

    def test_visible_receipt_dialog_can_be_verified_independently_of_the_history_backdrop(self):
        dialog = Scope(receipt(), ("운임영수증",))
        page = Page("다른 이용내역이 있는 목록", headings=("이용내역/영수증 조회",), dialogs=(dialog,))
        self.assertEqual(inspect_korail_receipt(page, request())["receipt_screen"], "영수증 대화상자")
        self.assertIs(korail_receipt_target(page), dialog)

    def test_scroll_clipped_receipt_dialog_requires_full_pdf_instead_of_partial_capture(self):
        dialog = Scope(receipt(), ("운임영수증",))
        dialog.scroll_clipped = True
        page = Page("이용내역 목록", headings=("이용내역/영수증 조회",), dialogs=(dialog,))
        with self.assertRaisesRegex(KorailReceiptError, "전체 영수증을 PDF"):
            korail_receipt_target(page)

    def test_wrong_travel_date_cannot_match_a_payment_date_in_the_requested_period(self):
        body = receipt(travel_date="2026-09-30") + "결제일 2026-10-20\n"
        with self.assertRaisesRegex(KorailReceiptError, "출장 기간"):
            inspect_korail_receipt(Page(body), request())

    def test_payment_date_alone_does_not_establish_the_travel_date(self):
        body = receipt().replace("승차일 2026-10-20", "결제일 2026-10-20")
        with self.assertRaisesRegex(KorailReceiptError, "승차일"):
            inspect_korail_receipt(Page(body), request())

    def test_wrong_route_or_missing_requested_stations_is_rejected(self):
        with self.assertRaisesRegex(KorailReceiptError, "승차구간"):
            inspect_korail_receipt(Page(receipt(destination="부산")), request())
        with self.assertRaisesRegex(KorailReceiptError, "철도 출발역"):
            inspect_korail_receipt(Page(receipt()), request(rail_origin=""))

    def test_multiple_receipts_need_separate_selection(self):
        page = Page(receipt() + receipt(travel_date="2026-10-21", origin="오송", destination="서울"))
        with self.assertRaises(KorailReceiptError):
            inspect_korail_receipt(page, request())

    def test_login_or_visible_password_fields_are_never_captured_as_receipts(self):
        for page in (Page(receipt(), url="https://www.korail.com/ticket/login"),
                     Page(receipt(), passwords=(Field(field_type="password"),))):
            with self.subTest(url=page.url), self.assertRaisesRegex(KorailReceiptError, "로그인"):
                inspect_korail_receipt(page, request())

    def test_host_lookalikes_are_rejected_but_an_official_opener_print_popup_is_allowed(self):
        for url in ("https://korail.com.evil.test/receipt", "https://notkorail.com/receipt"):
            with self.subTest(url=url), self.assertRaises(KorailReceiptError):
                inspect_korail_receipt(Page(receipt(), url=url), request())
        popup = Page(receipt(), url="about:blank", opener=Page("공식 조회 화면"))
        self.assertEqual(inspect_korail_receipt(popup, request())["rail_origin"], "서울")

    def test_worker_tracked_noopener_output_can_be_verified_without_loosening_default_provenance(self):
        popup = Page(receipt(), url="http://127.0.0.1/output", opener=None)
        with self.assertRaises(KorailReceiptError):
            inspect_korail_receipt(popup, request())
        with self.assertRaises(KorailReceiptError):
            korail_receipt_target(popup)
        metadata = inspect_korail_receipt(popup, request(), trusted_popup=True)
        self.assertEqual(metadata["승차일"], "2026-10-20")
        self.assertIs(korail_receipt_target(popup, trusted_popup=True), popup)

    def test_trusted_popup_only_replaces_provenance_not_receipt_date_route_or_login_checks(self):
        pages = (
            Page(receipt(), headings=("이용내역/영수증 조회",), url="about:blank"),
            Page(receipt(travel_date="2026-09-30"), url="about:blank"),
            Page(receipt(destination="부산"), url="about:blank"),
            Page(receipt(), url="about:blank", passwords=(Field(field_type="password"),)),
        )
        for page in pages:
            with self.subTest(text=page.text), self.assertRaises(KorailReceiptError):
                inspect_korail_receipt(page, request(), trusted_popup=True)
        dialog = Scope(receipt(), ("운임영수증",))
        dialog.scroll_clipped = True
        with self.assertRaisesRegex(KorailReceiptError, "전체 영수증을 PDF"):
            korail_receipt_target(Page("", headings=(), url="about:blank", dialogs=(dialog,)), trusted_popup=True)

    def test_receipt_heading_issuer_and_payment_details_are_all_required(self):
        cases = (
            Page(receipt(), headings=("결제 내역",)),
            Page(receipt().replace("한국철도공사 KORAIL", "가상 발행처")),
            Page(receipt().replace("결제수단 신용카드", "")),
            Page(receipt().replace("결제금액 18,500원", "결제금액 0원")),
            Page(receipt() + "환불 완료\n"),
        )
        for page in cases:
            with self.subTest(text=page.text), self.assertRaises(KorailReceiptError):
                inspect_korail_receipt(page, request())


class KorailPreparationTests(unittest.TestCase):
    def test_retry_reuses_an_existing_login_page(self):
        page = Page("코레일 회원 로그인\n회원번호와 비밀번호를 입력하세요.", headings=("로그인",),
                    url="https://www.korail.com/ticket/login", title="코레일 로그인", passwords=(Field(field_type="password"),))
        def unexpected_new_page():
            raise AssertionError("Retry must not create another login tab")
        prepared = prepare_korail(SimpleNamespace(pages=[page], new_page=unexpected_new_page), request(), lambda _message: None)
        self.assertIs(prepared.page, page)
        self.assertFalse(prepared.ready)
        self.assertEqual(page.visits, [KORAIL_RECEIPT_URL])

    def test_a_matching_existing_individual_receipt_is_reused_without_navigation(self):
        page = Page(receipt())
        def unexpected_new_page():
            raise AssertionError("A verified receipt must stay open")
        prepared = prepare_korail(SimpleNamespace(pages=[page], new_page=unexpected_new_page), request(), lambda _message: None)
        self.assertIs(prepared.page, page)
        self.assertTrue(prepared.ready)
        self.assertEqual(page.visits, [])
        self.assertEqual(prepared.details["승차일"], "2026-10-20")

    def test_entry_requires_manual_login_and_does_not_submit_credentials(self):
        page = Page("회원번호\n비밀번호\n코레일 회원 로그인", headings=("로그인",),
                    title="코레일 로그인", passwords=(Field(field_type="password"),))
        context = SimpleNamespace(new_page=lambda: page)
        prepared = prepare_korail(context, request(), lambda _message: None)
        self.assertFalse(prepared.ready)
        self.assertEqual(page.visits, [KORAIL_RECEIPT_URL])
        self.assertIn("직접 로그인", prepared.message)
        self.assertEqual(page.passwords[0].fills, [])

    def test_history_filters_are_only_filled_when_labels_and_date_formats_are_clear(self):
        start, end = Field(field_type="date"), Field(placeholder="YYYY.MM.DD")
        origin, destination = Field(), Field()
        page = Page("코레일 이용내역/영수증 조회\n조회기간을 지정하고 조회하세요.", headings=("이용내역/영수증 조회",),
                    labels={"시작일": (start,), "종료일": (end,), "출발역": (origin,), "도착역": (destination,)})
        prepared = prepare_korail(SimpleNamespace(new_page=lambda: page), request(), lambda _message: None)
        self.assertFalse(prepared.ready)
        self.assertEqual(start.fills, ["2026-10-19"])
        self.assertEqual(end.fills, ["2026.10.21"])
        self.assertEqual(origin.fills, ["서울역"])
        self.assertEqual(destination.fills, ["오송역"])
        self.assertIn("개별 운임영수증", prepared.message)

    def test_reservation_inputs_and_unclear_date_formats_stay_untouched(self):
        field = Field()
        page = Page("승차권 예매\n출발역과 가는 날을 선택하세요.", headings=("승차권 예매",),
                    url="https://www.korail.com/ticket/main", labels={"출발역": (field,), "시작일": (field,)})
        prepared = prepare_korail(SimpleNamespace(new_page=lambda: page), request(), lambda _message: None)
        self.assertFalse(prepared.ready)
        self.assertEqual(field.fills, [])


if __name__ == "__main__":
    unittest.main()
