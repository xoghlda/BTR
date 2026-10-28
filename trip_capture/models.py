from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Callable


SITE_LABELS = {"naver": "네이버 지도", "opinet": "오피넷 일일 유가", "hipass": "하이패스 영수증", "korail": "코레일 KTX 영수증"}
FUEL_LABELS = {"diesel": "자동차용경유", "gasoline": "보통휘발유", "premium": "고급휘발유", "kerosene": "실내등유"}
TRANSPORT_LABELS = {"car": "자차 이용", "train": "기차 이용"}
TRANSPORT_SITES = {"car": ("naver", "opinet", "hipass"), "train": ("korail",)}


@dataclass(frozen=True)
class RunRequest:
    trip_date: str
    origin: str
    destination: str
    fuel: str = "diesel"
    sites: tuple[str, ...] = ("naver", "opinet", "hipass")
    output_dir: Path = field(default_factory=lambda: Path.home() / "Documents" / "출장증빙")
    browser: str = "chrome"
    hipass_id: str = ""
    use_saved_password: bool = False
    trip_end_date: str = ""
    department: str = ""
    position: str = ""
    traveler_name: str = ""
    trip_purpose: str = ""
    rail_origin: str = ""
    rail_destination: str = ""
    rail_return_origin: str = ""
    rail_return_destination: str = ""
    transport_mode: str = ""

    @property
    def resolved_transport_mode(self) -> str:
        if self.transport_mode in TRANSPORT_LABELS:
            return self.transport_mode
        return "train" if "korail" in self.sites else "car"

    def validate(self) -> None:
        if self.transport_mode not in ("", "car", "train"):
            raise ValueError("출장 유형은 자차 이용 또는 기차 이용을 선택해 주세요.")
        if self.transport_mode and any(site not in TRANSPORT_SITES[self.transport_mode] for site in self.sites):
            raise ValueError("선택한 출장 유형에 해당하는 사이트만 사용할 수 있습니다.")
        parsed = date.fromisoformat(self.trip_date)
        if parsed > date.today():
            raise ValueError("출장일은 오늘 또는 과거 날짜로 입력해 주세요.")
        end = date.fromisoformat(self.trip_end_date) if self.trip_end_date else parsed
        if end < parsed:
            raise ValueError("출장 종료일은 시작일보다 빠를 수 없습니다.")
        if end > date.today():
            raise ValueError("출장 종료일은 오늘 또는 과거 날짜로 입력해 주세요.")
        if not self.sites or any(site not in SITE_LABELS for site in self.sites):
            raise ValueError("캡처할 사이트를 한 개 이상 선택해 주세요.")
        if "naver" in self.sites and (not self.origin.strip() or not self.destination.strip()):
            raise ValueError("네이버 지도 캡처에는 출발지와 도착지가 필요합니다.")
        if self.fuel not in FUEL_LABELS:
            raise ValueError("지원하지 않는 유종입니다.")
        if self.browser not in ("chrome", "msedge"):
            raise ValueError("브라우저는 Chrome 또는 Edge를 선택해 주세요.")


@dataclass
class PreparedPage:
    page: Any
    ready: bool = False
    message: str = ""
    details: dict[str, str] = field(default_factory=dict)


@dataclass
class CaptureRecord:
    site: str
    title: str
    image_path: Path
    url: str
    captured_at: str
    details: dict[str, str] = field(default_factory=dict)
    manual: bool = False
    original_path: Path | None = None


LogCallback = Callable[[str], None]
