# BTR · 출장 증빙 캡처

자차·기차 출장에 필요한 원본 화면과 영수증을 모아 HTML 보고서와 A4 정산 증빙 서식을 만드는 Windows 프로그램입니다. 현재 소스 버전은 **v1.3.1**입니다.

## 수집 방식

| 출장 유형 | 수집 자료 | 결과 서식 |
| --- | --- | --- |
| 자차 이용 | 네이버 지도 경로, 오피넷 일일 유가, 하이패스 영수증 | `car_settlement.html` |
| 기차 이용 | 코레일 영수증 PDF 또는 사용자가 선택한 영수증·출력 창 | `rail_settlement.html` |

공통 출장정보와 기간을 입력하고 유형을 선택합니다. PDF를 가져오면 원본 파일을 보관하고 모든 페이지를 이미지로 첨부합니다. 여러 영수증을 추가할 수 있으며, 기차 수집은 자료를 추가한 뒤 **코레일 수집 완료**로 마칩니다.

코레일은 Windows 기본 브라우저를 열어 사용자가 직접 로그인·조회합니다. 기차 수집에는 Playwright 브라우저 실행이 필요하지 않습니다. 실제 계정의 로그인 차단이 해결됐는지는 검증되지 않았습니다.

## 소스 실행

- Windows, Python 3.12
- 자차 자동 조회에는 Chrome 또는 Microsoft Edge 설치 필요

프로젝트 폴더에서 실행합니다.

```powershell
python -m pip install -r requirements.txt
python main.py
```

`실행.cmd`도 사용할 수 있습니다. 실행 파일이 있으면 최신 버전을 먼저 실행하며, 실행 파일이 없으면 Python 소스를 실행합니다.

자세한 조작 순서는 [사용방법.txt](사용방법.txt)를 참고하세요.

## EXE 빌드

```powershell
python -m pip install -r requirements-dev.txt
.\build.ps1
```

생성 파일: `dist/출장증빙캡처_v1.3.1.exe`

이 저장소에는 소스와 빌드 스크립트가 포함되어 있습니다. 실행 파일은 빌드해 사용할 수 있습니다.

## 테스트

```powershell
python -m unittest discover -s tests -q
```

단위 테스트는 가상 PDF·이미지와 모의 브라우저를 사용합니다. 실제 계정 인증 성공을 검증하는 테스트는 아닙니다. `tools/`에는 공개 사이트와 보고서 흐름을 확인하는 개발용 도구가 있습니다.

## 저장되는 자료

선택한 결과 폴더에 원본 PNG, 가져온 PDF, `travel_report.html`, 유형별 정산 서식, `manifest.json`을 저장합니다. 화면·금액을 임의로 재작성하지 않으며 사용자 입력 정보는 보고서에 구분해 표시합니다.

PC 설정과 자동화 브라우저 프로필은 `%LOCALAPPDATA%/TripEvidenceCapture`에 저장됩니다. 선택적으로 저장하는 하이패스 비밀번호는 Windows 자격 증명에 보관하며 코레일 비밀번호 입력·저장 기능은 없습니다. 생성된 보고서, 브라우저 프로필, 로그인 설정, 빌드 산출물은 `.gitignore`에서 제외합니다.
