# BTR v1.4.0 · Edge 버튼 클릭 매크로

**[BTR.exe 다운로드](https://github.com/xoghlda/BTR/raw/refs/heads/main/releases/v1.4.0/windows-x64/BTR.exe)** · [ZIP 패키지](https://github.com/xoghlda/BTR/releases/download/v1.4.0/BTR-v1.4.0-windows-x64.zip)

Windows 64비트용 실행파일입니다. Python 설치 없이 실행하며, v1.3.1의 증빙 수집에 **브라우저 버튼 매크로**를 추가했습니다.

## Edge에서 먼저 연습하기

1. ZIP을 모두 풀고 `BTR.exe`를 실행합니다.
2. Microsoft Edge로 `samples/macro-practice.html`을 엽니다. 로그인·인터넷 연결 없이 시험하는 개발용 페이지입니다.
3. BTR 오른쪽 위의 **브라우저 버튼 매크로**를 누르고 연습 화면을 연 Edge 창을 직접 선택합니다.
4. **4초 후 포인터 위치 등록**을 누르고, 선택한 Edge 창을 맨 앞으로 가져와 연습 버튼 위에 포인터를 두세요. 세 버튼을 각각 등록합니다.
5. 페이지를 새로고침해 횟수를 0으로 만든 뒤 **4초 후 실행**을 누릅니다. 4초 안에 Edge 창의 빈 영역을 클릭해 앞으로 가져오세요.
6. 각 버튼의 횟수가 1씩 증가하는지 확인합니다. **Esc** 또는 주 모니터 왼쪽 위로 포인터를 이동해 멈출 수 있습니다.

창 크기·배율·스크롤·페이지 배치를 유지하세요. 실제 사이트에서는 직접 로그인·인증한 뒤 조회·영수증 버튼의 위치를 등록합니다. 다른 창이나 새 출력 앱으로 넘어가는 동작은 창을 다시 선택해야 합니다.

매크로는 **좌표를 클릭할 뿐 조회·영수증 발급 성공을 판정하지 않습니다.** 마친 뒤 매크로 창을 닫고 BTR의 PDF 가져오기·출력 창 저장으로 자료를 모으세요.

## 포함 파일

- `BTR.exe`: v1.4.0 실행파일
- `사용방법.txt`, `빠른테스트.txt`: 한글 안내
- `samples/macro-practice.html`: 버튼 3개의 클릭 횟수로 확인하는 연습 화면
- `samples/DEVELOPMENT_ONLY_SYNTHETIC_3PAGES.pdf`: 기존 PDF 수집용 개발 예제
- `BUILDINFO.json`: 앱 소스 커밋·빌드·검증 범위
- `SHA256SUMS-in-package.txt`: 위 배포 파일 6개의 검증값

소스 테스트 171개와 EXE의 매크로·PDF·정산서 자체 검사가 통과했습니다. 매크로 자체 검사는 가짜 어댑터로 수행했으며 실제 Edge 사이트 입력·인증 성공은 검증하지 않았습니다. 코드 서명은 없습니다.

실행파일 SHA-256:

```text
b73a63ac493170171c8423f902705028748fe2b974052a7e9d26f15c13e17e9d
```

[상세 사용법](../../../README.md) · [버전 목록](../../README.md)
