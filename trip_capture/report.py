"""Build an offline, printable report without changing screenshot pixels."""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .models import CaptureRecord, FUEL_LABELS, RunRequest, SITE_LABELS


_REDACTED = "[기록 제외]"
_SENSITIVE_PARTS = (
    "auth", "session", "token", "password", "passwd", "pwd", "secret",
    "credential", "apikey", "accesskey", "privatekey", "signature",
    "account", "userid", "username", "loginid", "memberid", "hipassid",
    "cardnumber", "cardno", "비밀번호", "패스워드", "계정", "아이디", "인증키",
)
_SENSITIVE_EXACT = {"key", "sid", "jsessionid", "phpsessid", "jwt", "cookie", "code", "ticket", "tx", "state", "nonce", "csrf"}
_DETAIL_LABELS = {
    "trip_date": "조회 요청일", "fuel": "유종", "verified_date": "화면에서 확인한 날짜",
    "price_scope": "가격 기준", "origin": "출발지 요청", "destination": "도착지 요청",
    "selected_origin": "선택된 출발지", "selected_destination": "선택된 도착지",
    "route_time_basis": "경로 기준", "vehicle_fuel": "지도 요금 계산 유종",
    "route_fuel": "지도 요금 계산 유종", "route_vehicle": "지도 차량 기준", "route_fuel_status": "지도 유종 확인 사항",
}
_CAR_SITES = ("naver", "opinet", "hipass")
_CAR_CATEGORIES = {"naver": "경로·소요거리", "opinet": "일일 유가", "hipass": "통행료 영수증"}
_TRANSPORT_LABELS = {"car": "자차 이용", "train": "기차 이용"}


def _sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[\s_\-.\[\]]", "", str(key)).casefold()
    return normalized in _SENSITIVE_EXACT or any(part in normalized for part in _SENSITIVE_PARTS)


def sanitize_source_url(url: str) -> str:
    """Keep a useful source URL while removing credentials and session material.

    Fragments are omitted because login flows commonly put tokens there. This
    function is also exported for capture logs and other metadata writers.
    """
    try:
        parts = urlsplit(str(url).strip())
        if parts.scheme.lower() not in {"http", "https"} or not parts.hostname:
            return ""
        # Drop user:password@ even when the credentials are percent-encoded.
        hostname = parts.hostname
        host = f"[{hostname}]" if ":" in hostname else hostname
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        query = urlencode([
            (key, _REDACTED if _sensitive_key(key) else value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
        ])
        # Some sites put session identifiers in path parameters instead.
        path = re.sub(
            r";(?:jsessionid|phpsessid|sessionid|sid)=[^/;?]*",
            ";session=" + _REDACTED,
            parts.path,
            flags=re.IGNORECASE,
        )
        return urlunsplit((parts.scheme.lower(), host, path, query, ""))
    except (TypeError, ValueError):
        return ""


def _escape(value: object) -> str:
    return html.escape(str(value), quote=True)


def _date_label(value: str) -> str:
    try:
        parsed = date.fromisoformat(value)
        return f"{parsed.year}년 {parsed.month}월 {parsed.day}일"
    except (TypeError, ValueError):
        return str(value)


def _capture_time_label(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        label = f"{parsed.year}년 {parsed.month}월 {parsed.day}일 {parsed:%H:%M:%S}"
        offset = parsed.utcoffset()
        if offset is None:
            return label + " (시간대 미기록)"
        minutes = int(offset.total_seconds() / 60)
        sign = "+" if minutes >= 0 else "-"
        minutes = abs(minutes)
        return f"{label} (UTC{sign}{minutes // 60:02d}:{minutes % 60:02d})"
    except (AttributeError, TypeError, ValueError):
        return str(value)


def _safe_details(details: dict[str, str]) -> dict[str, str]:
    safe: dict[str, str] = {}
    for key, value in details.items():
        label, text = str(key), str(value)
        if _sensitive_key(label):
            safe[label] = _REDACTED
        elif "url" in label.casefold() or text.lstrip().casefold().startswith(("https://", "http://")):
            # Site adapters also place the current URL in their details. Never
            # let that duplicate bypass the primary source URL's redaction.
            safe[label] = sanitize_source_url(text)
        else:
            safe[label] = text
    return safe


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    raise ValueError("지원하는 화면 이미지(PNG, JPEG, GIF, WebP)가 아닙니다.")


def _pdf_attachment(path: Path, output_dir: Path) -> dict[str, object]:
    """Describe a local original without loading its entire content into HTML."""
    checksum = hashlib.sha256()
    size = 0
    with path.open("rb") as file:
        first = file.read(1024)
        if b"%PDF-" not in first:
            raise ValueError("원본 파일이 PDF 형식이 아닙니다.")
        checksum.update(first)
        size += len(first)
        while chunk := file.read(1024 * 1024):
            checksum.update(chunk)
            size += len(chunk)
    return {
        "file": path.name,
        "path": Path(os.path.relpath(path.resolve(), output_dir.resolve())).as_posix(),
        "mime": "application/pdf",
        "bytes": size,
        "sha256": checksum.hexdigest(),
    }


def _request_text(request: RunRequest, name: str) -> str:
    value = getattr(request, name, "")
    return "" if value is None else str(value)


def _trip_period(request: RunRequest) -> str:
    start = _date_label(request.trip_date)
    end = _request_text(request, "trip_end_date").strip()
    return f"{start} ~ {_date_label(end)}" if end and end != request.trip_date else start


def _transport_mode(request: RunRequest) -> str:
    resolved = getattr(request, "resolved_transport_mode", "")
    if resolved in _TRANSPORT_LABELS:
        return resolved
    explicit = _request_text(request, "transport_mode")
    return explicit if explicit in _TRANSPORT_LABELS else ("train" if "korail" in request.sites else "car")


def _return_stations(request: RunRequest) -> tuple[str, str]:
    return (
        _request_text(request, "rail_return_origin").strip() or _request_text(request, "rail_destination").strip(),
        _request_text(request, "rail_return_destination").strip() or _request_text(request, "rail_origin").strip(),
    )


def _mode_trip_rows(request: RunRequest, mode: str) -> str:
    rows = [
        ("이용 유형", _TRANSPORT_LABELS[mode]),
        ("소속", _request_text(request, "department")),
        ("직급", _request_text(request, "position")),
        ("성명", _request_text(request, "traveler_name")),
        ("출장기간", _trip_period(request)),
        ("출장지", request.destination),
        ("출장 목적", _request_text(request, "trip_purpose")),
    ]
    if mode == "car":
        rows.extend((("출발지", request.origin), ("도착지", request.destination), ("유종", FUEL_LABELS.get(request.fuel, request.fuel))))
    else:
        return_origin, return_destination = _return_stations(request)
        rows.extend((
            ("가는 편 요청역", f"{_request_text(request, 'rail_origin') or '미입력'} → {_request_text(request, 'rail_destination') or '미입력'}"),
            ("오는 편 요청역", f"{return_origin or '미입력'} → {return_destination or '미입력'}"),
        ))
    return "".join(f"<tr><th>{_escape(label)}</th><td>{_escape(value or '미입력')}</td></tr>" for label, value in rows)


def _write_car_settlement(
    request: RunRequest,
    evidence: list[tuple[CaptureRecord, str]],
    output_dir: Path,
    counts: dict[str, int],
    failed_counts: dict[str, int],
) -> Path:
    """Place each original car evidence image on its own printable A4 sheet."""
    coverage: list[str] = []
    for site in _CAR_SITES:
        if counts.get(site, 0):
            status = f"{counts[site]}개 보관"
            if failed_counts.get(site, 0):
                status += f" · 이미지 누락 {failed_counts[site]}개"
        elif site in request.sites or failed_counts.get(site, 0):
            status = "누락 · 저장된 자료 없음"
        else:
            status = "선택하지 않음"
        coverage.append(f"{_CAR_CATEGORIES[site]}: {status}")
    pages = evidence or [None]
    sheets: list[str] = []
    for index, item in enumerate(pages, start=1):
        if item is None:
            evidence_markup = '<h2>자차 증빙 자료 없음</h2><p class="empty">선택한 지도·유가·통행료 자료가 저장되지 않았습니다. 원본 자료를 추가한 뒤 확인하세요.</p>'
        else:
            record, uri = item
            image_kind = "원본 PDF 페이지 변환 이미지" if record.original_path is not None else "원본 화면 캡처"
            evidence_markup = (
                f'<h2>{index}. {_escape(_CAR_CATEGORIES.get(record.site, record.site))} · {_escape(record.title)}</h2>'
                f'<p class="source">{image_kind} · 자료 저장 시각: {_escape(_capture_time_label(record.captured_at))}</p>'
                f'<figure><img src="{uri}" alt="{_escape(record.title)} 원본 자료"></figure>'
            )
        sheets.append(f"""
<section class="sheet" aria-label="자차 증빙서 {index}/{len(pages)}쪽">
  <h1>자차 이용 출장 증빙서</h1>
  <table class="trip"><caption>출장 정보 · 사용자 입력</caption><tbody>
    <tr><th>소속</th><td>{_escape(_request_text(request, 'department'))}</td><th>직급</th><td>{_escape(_request_text(request, 'position'))}</td><th>성명</th><td>{_escape(_request_text(request, 'traveler_name'))}</td></tr>
    <tr><th>출장기간</th><td colspan="5">{_escape(_trip_period(request))}</td></tr>
    <tr><th>출장 목적</th><td colspan="5">{_escape(_request_text(request, 'trip_purpose'))}</td></tr>
    <tr><th>출발지</th><td colspan="2">{_escape(request.origin)}</td><th>도착지</th><td colspan="2">{_escape(request.destination)}</td></tr>
    <tr><th>유종</th><td colspan="5">{_escape(FUEL_LABELS.get(request.fuel, request.fuel))}</td></tr>
  </tbody></table>
  <p class="coverage">{_escape(' / '.join(coverage))}</p>
  <p class="explanation">거리·소요시간·유가·통행료는 아래 원본 자료에서 확인하세요. 교통비 합계는 임의로 산출하지 않습니다. 지도는 조회 시점의 참고 경로이며 과거의 실제 운행을 증명하지 않습니다.</p>
  {evidence_markup}
  <footer>원본 이미지를 내용 편집 없이 배치했습니다. 원본 PDF·출처·저장 시각은 메인 보고서에서 확인하세요. <span>{index} / {len(pages)}쪽</span></footer>
</section>""")
    content = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>자차 이용 출장 증빙서</title><style>
@page {{ size: A4; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #111; background: #eef0f3; font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; font-size: 9pt; line-height: 1.35; }}
.navigation {{ width: 186mm; margin: 15px auto; }}
.sheet {{ width: 186mm; min-height: 265mm; margin: 14px auto; padding: 0; background: white; }}
h1 {{ margin: 0 0 5mm; padding-top: 3mm; text-align: center; font-size: 20pt; }}
h2 {{ font-size: 11pt; margin: 3mm 0 1mm; overflow-wrap: anywhere; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; margin-bottom: 3mm; }}
caption {{ text-align: left; font-weight: bold; margin-bottom: 1.5mm; }}
th, td {{ border: 1px solid #222; padding: 1.5mm 2mm; text-align: center; overflow-wrap: anywhere; }}
th {{ width: 12%; background: #f4f4f4; font-weight: 600; }} td {{ width: 21.3%; }}
.coverage, .explanation {{ font-size: 7.5pt; margin: 2mm 0; }} .source {{ font-size: 7pt; margin: 1mm 0 2mm; color: #555; }}
figure {{ margin: 0; height: 145mm; display: flex; justify-content: center; align-items: flex-start; }}
figure img {{ display: block; width: auto; height: auto; max-width: 100%; max-height: 145mm; object-fit: contain; }}
footer {{ display: flex; justify-content: space-between; gap: 3mm; margin-top: 4mm; padding-top: 2mm; border-top: 1px solid #999; font-size: 7pt; }}
.empty {{ padding: 6mm; border: 1px solid #b3755b; color: #8a250a; }}
@media print {{ body {{ background: white; }} .navigation {{ display: none; }}
 .sheet {{ margin: 0; min-height: 0; break-after: page; }} .sheet:last-child {{ break-after: auto; }}
 table, figure, footer {{ break-inside: avoid; }} }}
</style></head><body><p class="navigation"><a href="travel_report.html">메인 보고서로 돌아가기</a> · Ctrl+P로 인쇄하거나 PDF로 저장하세요.</p>{''.join(sheets)}</body></html>"""
    path = output_dir / "car_settlement.html"
    path.write_text(content, encoding="utf-8")
    return path


def _rail_field(confirmed: str, entered: str, entered_label: str) -> str:
    if confirmed.strip():
        return f'{_escape(confirmed)}<small class="basis">영수증 화면 확인</small>'
    if entered.strip():
        return f'{_escape(entered)}<small class="basis">사용자 입력 {entered_label}</small>'
    return '<span class="unconfirmed">확인 필요</span><small class="basis">역 정보 미입력</small>'


def _write_rail_settlement(
    request: RunRequest,
    evidence: list[tuple[CaptureRecord, str]],
    output_dir: Path,
    omitted: int = 0,
) -> Path:
    """Arrange original images; never draw or reconstruct a receipt."""
    chunks = [evidence[index:index + 2] for index in range(0, len(evidence), 2)] or [[]]
    sheets: list[str] = []
    return_origin, return_destination = _return_stations(request)
    return_note = (
        '<span class="return-route">귀환 요청 경로(사용자 입력 기준): '
        f'{_escape(return_origin or "미입력")} → {_escape(return_destination or "미입력")}. '
        'PDF 페이지별 방향은 자동 지정하지 않습니다.</span>'
        if return_origin or return_destination else ""
    )
    for sheet_index, chunk in enumerate(chunks, start=1):
        rows: list[str] = []
        figures: list[str] = []
        for position, (record, image_uri) in enumerate(chunk):
            number = (sheet_index - 1) * 2 + position + 1
            details = _safe_details(record.details)
            confirmed = {key: details.get(key, "").strip() for key in ("승차일", "출발역", "도착역")}
            day = _rail_field(confirmed["승차일"], _trip_period(request), "출장기간")
            origin = _rail_field(confirmed["출발역"], _request_text(request, "rail_origin"), "출발역")
            destination = _rail_field(confirmed["도착역"], _request_text(request, "rail_destination"), "도착역")
            basis = "승차 정보 화면 확인" if all(confirmed.values()) else "사용자 입력 또는 미확인 항목 포함"
            page_label = details.get("PDF 페이지", "")
            note = f"자료 {number} · {basis}" + (f" · PDF {page_label}" if page_label else "")
            rows.append(f"<tr><td>{day}</td><td>철도(코레일)</td><td>{origin}</td><td>{destination}</td><td>{_escape(note)}</td></tr>")
            image_kind = "원본 PDF 페이지 변환 이미지" if record.original_path is not None else "원본 화면 캡처"
            figures.append(
                f'<figure class="receipt"><figcaption>자료 {number} · {_escape(record.title)}'
                f'<small>{image_kind}</small></figcaption>'
                f'<div class="receipt-image"><img src="{image_uri}" alt="자료 {number}: {_escape(record.title)}"></div></figure>'
            )
        if not rows:
            rows.append('<tr><td colspan="5" class="empty">저장된 코레일 영수증이 없습니다. 자료를 추가한 뒤 정산서를 확인하세요.</td></tr>')
        if not figures:
            figures.append('<p class="empty">첨부할 영수증 이미지가 없습니다.</p>')
        omitted_note = f'<p class="missing">읽을 수 없는 코레일 이미지 {omitted}개가 있습니다. 메인 보고서의 실패 내역을 확인하세요.</p>' if omitted else ""
        sheets.append(f"""
<section class="sheet" aria-label="정산서 {sheet_index}/{len(chunks)}쪽">
  <h1>출장운임정산 증빙영수증</h1>
  <table class="traveler"><caption>출장 정보 · 사용자 입력</caption><tbody>
    <tr><th>소속</th><td>{_escape(_request_text(request, 'department'))}</td><th>직급</th><td>{_escape(_request_text(request, 'position'))}</td><th>성명</th><td>{_escape(_request_text(request, 'traveler_name'))}</td></tr>
    <tr><th>출장지</th><td colspan="5">{_escape(request.destination)}</td></tr>
    <tr><th>출장기간</th><td colspan="5">{_escape(_trip_period(request))}</td></tr>
    <tr><th>목적</th><td colspan="5">{_escape(_request_text(request, 'trip_purpose'))}</td></tr>
  </tbody></table>
  <table class="fares"><caption>철도 운임 증빙 자료</caption><thead><tr><th>일자</th><th>교통편</th><th>출발지</th><th>도착지</th><th>비고</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
  <p class="explanation">‘영수증 화면 확인’은 저장 당시 확인된 승차일·역 정보입니다. ‘사용자 입력’은 출장기간 또는 요청한 역이며 실제 승차 정보와 대조해야 합니다. PDF 여러 페이지는 각각 첨부 자료이며 별도 승차 건수를 뜻하지 않습니다.{return_note}</p>
  {omitted_note}
  <div class="receipts">{''.join(figures)}</div>
  <footer>원본 이미지를 내용 편집 없이 배치했습니다. 원본 PDF·출처·저장 시각은 메인 보고서에서 확인하세요. <span>{sheet_index} / {len(chunks)}쪽</span></footer>
</section>""")
    content = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>출장운임정산 증빙영수증</title><style>
@page {{ size: A4; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0; color: #111; background: #eef0f3; font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; font-size: 9pt; line-height: 1.35; }}
.navigation {{ width: 186mm; margin: 15px auto; }}
.sheet {{ width: 186mm; min-height: 265mm; margin: 14px auto; padding: 0; background: white; }}
h1 {{ margin: 0 0 5mm; padding-top: 3mm; text-align: center; font-size: 20pt; letter-spacing: 0.4mm; }}
table {{ width: 100%; border-collapse: collapse; table-layout: fixed; margin-bottom: 3mm; }}
caption {{ text-align: left; font-weight: bold; margin-bottom: 1.5mm; }}
th, td {{ border: 1px solid #222; padding: 1.5mm 2mm; text-align: center; vertical-align: middle; overflow-wrap: anywhere; }}
th {{ background: #f4f4f4; font-weight: 600; }}
.traveler th {{ width: 11%; }} .traveler td {{ width: 22.3%; }}
.fares th:nth-child(1) {{ width: 23%; }} .fares th:nth-child(2) {{ width: 14%; }}
.fares th:nth-child(3), .fares th:nth-child(4) {{ width: 18%; }} .fares th:nth-child(5) {{ width: 27%; }}
.basis {{ display: block; font-size: 7pt; color: #555; margin-top: 1mm; }}
.explanation {{ margin: 2mm 0 3mm; font-size: 7.5pt; }}
.return-route {{ display: block; margin-top: 1mm; }}
.receipts {{ display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 4mm; align-items: start; }}
.receipt {{ margin: 0; min-width: 0; }}
figcaption {{ font-weight: 600; text-align: center; margin: 0 0 2mm; font-size: 8pt; overflow-wrap: anywhere; }}
figcaption small {{ display: block; color: #555; font-size: 7pt; font-weight: normal; }}
.receipt-image {{ display: flex; align-items: flex-start; justify-content: center; height: 122mm; }}
.receipt img {{ display: block; width: auto; height: auto; max-width: 100%; max-height: 122mm; object-fit: contain; }}
footer {{ margin-top: 4mm; border-top: 1px solid #999; padding-top: 2mm; font-size: 7pt; }} footer span {{ float: right; }}
.empty, .missing {{ padding: 3mm; color: #8a250a; }}
@media print {{
  body {{ background: white; }} .navigation {{ display: none; }}
  .sheet {{ margin: 0; min-height: 0; break-after: page; }} .sheet:last-child {{ break-after: auto; }}
  table, .receipt, footer {{ break-inside: avoid; }}
}}
</style></head><body><p class="navigation"><a href="travel_report.html">메인 보고서로 돌아가기</a> · Ctrl+P로 인쇄하거나 PDF로 저장하세요.</p>{''.join(sheets)}</body></html>
"""
    path = output_dir / "rail_settlement.html"
    path.write_text(content, encoding="utf-8")
    return path


def write_report(
    request: RunRequest,
    records: list[CaptureRecord],
    output_dir: Path,
    notices: list[str] | None = None,
) -> Path:
    """Write a transport-specific main report, A4 evidence form and manifest.

    Only explicitly permitted request fields enter either output. In particular,
    account identifiers and saved-password settings are never serialized.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    mode = _transport_mode(request)
    mode_sites = _CAR_SITES if mode == "car" else ("korail",)
    report_title = f"{_TRANSPORT_LABELS[mode]} 출장보고서"
    messages = [str(message) for message in (notices or [])]
    entries: list[dict[str, object]] = []
    sections: list[str] = []
    successful_sites: set[str] = set()
    attachments: dict[str, dict[str, object]] = {}
    attachment_errors: dict[str, str] = {}
    rail_evidence: list[tuple[CaptureRecord, str]] = []
    car_evidence: list[tuple[CaptureRecord, str]] = []
    site_counts: dict[str, int] = {}
    for index, record in enumerate(records, start=1):
        image_path = Path(record.image_path)
        source_url = sanitize_source_url(record.url)
        details = _safe_details(record.details)
        entry: dict[str, object] = {
            "site": str(record.site),
            "title": str(record.title),
            "image_file": image_path.name,
            "image_path": Path(os.path.relpath(image_path.resolve(), output_dir.resolve())).as_posix(),
            "source_url": source_url,
            "captured_at": str(record.captured_at),
            "manual": bool(record.manual),
            "details": details,
            "status": "failed",
            "transport_role": "primary" if record.site in mode_sites else "additional",
        }
        try:
            image_data = image_path.read_bytes()
            mime = _image_mime(image_data)
        except (OSError, ValueError) as exc:
            message = f"{SITE_LABELS.get(record.site, record.site)}: 화면 이미지를 읽지 못했습니다. ({type(exc).__name__})"
            messages.append(message)
            entry["error"] = message
            entries.append(entry)
            continue
        entry.update({
            "status": "captured",
            "image_mime": mime,
            "image_bytes": len(image_data),
            "image_sha256": hashlib.sha256(image_data).hexdigest(),
        })
        original_markup = ""
        is_pdf_page = record.original_path is not None
        if record.original_path is not None:
            original_path = Path(record.original_path)
            original_key = str(original_path.resolve())
            if original_key not in attachments and original_key not in attachment_errors:
                try:
                    attachments[original_key] = _pdf_attachment(original_path, output_dir)
                except (OSError, ValueError):
                    message = "원본 PDF 첨부를 읽을 수 없습니다. PDF 원본 파일을 결과 폴더에서 확인해 주세요."
                    attachment_errors[original_key] = message
                    messages.append(message)
            original = attachments.get(original_key)
            if original is not None:
                entry["original"] = original
                entry["kind"] = "pdf_page"
                original_href = _escape(quote(str(original["path"]), safe="/"))
                original_markup = (
                    '<p class="note">이 이미지는 화면 캡처가 아니라 원본 PDF의 페이지를 변환한 자료입니다. '
                    f'<a href="{original_href}" target="_blank" rel="noopener">원본 PDF 열기</a> · '
                    f'<a href="{original_href}" download="{_escape(original["file"])}">원본 PDF 다운로드</a></p>'
                    f'<p class="checksum">원본 PDF: {_escape(original["file"])} · SHA-256: {_escape(original["sha256"])}</p>'
                )
            else:
                entry["original_error"] = attachment_errors[original_key]
                original_markup = '<p class="note">원본 PDF 페이지 변환 이미지입니다. 원본 PDF 첨부를 읽을 수 없습니다.</p>'
        else:
            entry["kind"] = "screenshot"
        entries.append(entry)
        successful_sites.add(record.site)
        site_counts[record.site] = site_counts.get(record.site, 0) + 1
        detail_rows = "".join(
            f"<tr><th>{_escape(_DETAIL_LABELS.get(key, key))}</th><td>{_escape(value)}</td></tr>"
            for key, value in details.items()
            if key not in {"url", "requested_url", "source_url"}
            and not (mode == "train" and key in {"fuel", "vehicle_fuel", "route_fuel", "route_vehicle", "route_fuel_status"})
        )
        source_markup = (
            f'<a href="{_escape(source_url)}" rel="noreferrer">{_escape(source_url)}</a>'
            if source_url else ("사용자가 선택한 PDF 원본 첨부" if is_pdf_page else "출처 URL을 기록하지 못했습니다.")
        )
        naver_note = (
            '<p class="note">지도는 캡처 시점에 조회한 참고 경로입니다. '
            '과거 출장일의 실제 운행 경로나 당시 교통 상황을 증명하지 않습니다.</p>'
            if record.site == "naver" else ""
        )
        image_uri = f"data:{mime};base64,{base64.b64encode(image_data).decode('ascii')}"
        if record.site == "korail":
            rail_evidence.append((record, image_uri))
        if record.site in _CAR_SITES:
            car_evidence.append((record, image_uri))
        category = _CAR_CATEGORIES.get(record.site, "코레일 영수증") if record.site in mode_sites else "추가 보관 자료"
        extra_note = '<p class="note">이용 유형과 다른 기존 자료도 원본 보존을 위해 함께 포함했습니다.</p>' if record.site not in mode_sites else ""
        sections.append(f"""
<section class="capture" aria-labelledby="capture-{index}">
  <p class="evidence-category">{_escape(category)}</p>
  <h2 id="capture-{index}">{index}. {_escape(record.title)}</h2>
  {extra_note}
  <table class="metadata"><tbody>
    <tr><th>자료 구분</th><td>{_escape(SITE_LABELS.get(record.site, record.site))}</td></tr>
    <tr><th>{'PDF 가져온 시각' if is_pdf_page else '화면 캡처 시각'}</th><td>{_escape(_capture_time_label(record.captured_at))}</td></tr>
    <tr><th>수동 확인 여부</th><td>{'사용자가 선택한 PDF' if is_pdf_page else ('수동 확인 후 캡처' if record.manual else '자동 준비 후 캡처')}</td></tr>
    <tr><th>출처 URL</th><td class="source">{source_markup}</td></tr>
    {detail_rows}
  </tbody></table>
  {naver_note}
  {original_markup}
  <img class="screenshot" src="{image_uri}" alt="{_escape(record.title)} {'PDF 페이지 변환 이미지' if is_pdf_page else '원본 화면 캡처'}">
  <p class="checksum">{'변환 이미지' if is_pdf_page else '원본 화면 파일'}: {_escape(image_path.name)} · SHA-256: {_escape(entry['image_sha256'])}</p>
</section>""")

    for site in request.sites:
        if site not in successful_sites:
            messages.append(f"{SITE_LABELS.get(site, site)} 자료가 포함되지 않았습니다. 건너뛰기 또는 캡처 실패 내역을 확인해 주세요.")
    has_primary = any(site in successful_sites for site in mode_sites)
    if not has_primary:
        messages.insert(0, "자료 생성 실패: 해당 이용 유형으로 저장된 증빙이 없습니다. 이 보고서는 출장 증빙으로 사용할 수 없습니다.")
    status = "failed" if not has_primary else ("partial" if messages else "complete")
    notices_markup = "".join(f"<li>{_escape(message)}</li>" for message in messages)
    warning_section = (
        f'<aside class="notices"><h2>건너뛰기·실패 및 확인 사항</h2><ul>{notices_markup}</ul></aside>'
        if messages else ""
    )
    status_label = {"failed": "자료 생성 실패", "partial": "일부 자료 누락 또는 확인 사항 있음", "complete": "선택한 자료 캡처 완료"}[status]
    pdf_intro = (
        ' PDF 영수증은 원본 파일의 모든 페이지를 이미지로 변환해 포함했습니다. 원본 PDF 링크를 사용하려면 결과 폴더의 PDF 파일을 함께 보관하세요.'
        if attachments else ""
    )
    rail_path = None
    car_path = None
    failed_counts = {
        site: sum(record.site == site for record in records) - site_counts.get(site, 0)
        for site in (*_CAR_SITES, "korail")
    }
    if mode == "train":
        rail_path = _write_rail_settlement(
            request, rail_evidence, output_dir,
            omitted=sum(record.site == "korail" for record in records) - len(rail_evidence),
        )
        settlement_link = '<p><a href="rail_settlement.html">출장운임정산 증빙영수증 열기 · A4 인쇄/PDF 저장</a></p>'
    else:
        car_path = _write_car_settlement(request, car_evidence, output_dir, site_counts, failed_counts)
        settlement_link = '<p><a href="car_settlement.html">자차 이용 출장 증빙서 열기 · A4 인쇄/PDF 저장</a></p>'
    coverage = {
        site: {"requested": site in request.sites, "stored_images": site_counts.get(site, 0),
               "failed_images": failed_counts.get(site, 0),
               "status": ("partial" if failed_counts.get(site, 0) else "stored") if site_counts.get(site, 0)
               else ("missing" if site in request.sites or failed_counts.get(site, 0) else "not_selected")}
        for site in mode_sites
    }
    coverage_markup = " / ".join(
        _escape(f"{_CAR_CATEGORIES[site] if mode == 'car' else '코레일 영수증'}: " +
                ((f"{item['stored_images']}개 보관" + (f" · 이미지 누락 {item['failed_images']}개" if item['failed_images'] else ""))
                 if item['stored_images'] else ("누락" if item['status'] == 'missing' else "선택하지 않음")))
        for site, item in coverage.items()
    )
    evidence_heading = "경로·유가·통행료 증빙" if mode == "car" else "왕복역·코레일 영수증 증빙"
    report_html = f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{report_title} - {_escape(request.trip_date)}</title>
<style>
@page {{ size: A4; margin: 14mm; }}
* {{ box-sizing: border-box; }}
body {{ margin: 0 auto; padding: 24px; max-width: 1040px; color: #182334; background: white; font-family: "Malgun Gothic", "Apple SD Gothic Neo", sans-serif; font-size: 14px; line-height: 1.6; }}
h1 {{ font-size: 26px; margin-bottom: 8px; }} h2 {{ font-size: 18px; margin: 0 0 12px; }}
p {{ margin: 8px 0; }} a {{ color: #174a8b; }}
.intro, .note {{ color: #3d4c60; }} .status {{ font-weight: bold; }}
.evidence-category {{ color: #344b6d; font-size: 12px; font-weight: bold; }}
table {{ border-collapse: collapse; width: 100%; margin: 14px 0; table-layout: fixed; }}
th, td {{ border: 1px solid #cbd3df; text-align: left; padding: 7px 10px; vertical-align: top; overflow-wrap: anywhere; }}
th {{ width: 155px; background: #f1f4f8; font-weight: 600; }}
.capture {{ margin-top: 28px; padding-top: 18px; border-top: 2px solid #344b6d; }}
.screenshot {{ display: block; max-width: 100%; height: auto; margin: 12px auto; }}
.checksum {{ font-size: 10px; color: #536174; overflow-wrap: anywhere; }}
.notices {{ margin: 20px 0; padding: 14px 18px; background: #fff4dc; border: 1px solid #d0a247; }}
.notices ul {{ margin: 0; padding-left: 22px; }}
@media print {{
  body {{ padding: 0; max-width: none; font-size: 10pt; }}
  h1 {{ font-size: 20pt; }} h2 {{ font-size: 13pt; }}
  .capture {{ break-before: page; margin-top: 0; padding-top: 0; border-top: 0; }}
  table, .notices {{ break-inside: avoid; }}
  .screenshot {{ max-height: 205mm; width: auto; max-width: 100%; object-fit: contain; }}
  a {{ color: inherit; text-decoration: none; }}
}}
</style></head><body>
<h1>{report_title}</h1>
<p class="status">{status_label}</p>
{settlement_link}
<table><tbody>
  {_mode_trip_rows(request, mode)}
  <tr><th>포함된 화면</th><td>{len(sections)}개</td></tr>
</tbody></table>
<p class="intro">출장일은 사용자가 입력한 업무 날짜이며, 화면 캡처 시각 또는 PDF 가져온 시각은 자료를 저장한 시각입니다. 두 날짜는 다를 수 있습니다.
화면 캡처 이미지는 내용 편집 없이 포함했습니다.{pdf_intro} 사이트 화면과 PDF의 조회 기간 및 거래 내역은 제출 전에 확인해 주세요.</p>
{warning_section}
<h2>{evidence_heading}</h2><p>{coverage_markup}</p>
{''.join(sections)}
</body></html>
"""
    report_path = output_dir / "travel_report.html"
    report_path.write_text(report_html, encoding="utf-8")
    manifest = {
        "format_version": 1,
        "report_file": report_path.name,
        "status": status,
        "transport_mode": mode,
        "transport_label": _TRANSPORT_LABELS[mode],
        "trip": {
            "trip_date": request.trip_date,
            "origin": request.origin,
            "destination": request.destination,
            "transport_mode": mode,
            "transport_label": _TRANSPORT_LABELS[mode],
            **({"fuel": request.fuel, "fuel_label": FUEL_LABELS.get(request.fuel, request.fuel)} if mode == "car" else {}),
            "requested_sites": list(request.sites),
            **{name: _request_text(request, name) for name in (
                "trip_end_date", "department", "position", "traveler_name", "trip_purpose", "rail_origin", "rail_destination",
                "rail_return_origin", "rail_return_destination",
            )},
        },
        "captures": entries,
        "attachments": list(attachments.values()),
        "notices": messages,
        "rail_settlement_file": rail_path.name if rail_path else None,
        "car_settlement_file": car_path.name if car_path else None,
        "site_coverage": coverage,
    }
    manifest_json = json.dumps(manifest, ensure_ascii=False, indent=2)
    for character, escaped in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        manifest_json = manifest_json.replace(character, escaped)
    (output_dir / "manifest.json").write_text(manifest_json + "\n", encoding="utf-8")
    return report_path
